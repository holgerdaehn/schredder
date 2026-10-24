
# ───────────────────────── Linux ─────────────────────────
PLATFORM = "linux"
PREPARE_DESTROYS = False  # Aushängen verändert keine Daten
_SYSTEM = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "[SWAP]"}
_COLS = "NAME,PATH,SIZE,TYPE,ROTA,MODEL,SERIAL,TRAN,RM,MOUNTPOINTS,LABEL,FSTYPE"


def _mounts(node):
    m = [x for x in (node.get("mountpoints") or [node.get("mountpoint")]) if x]
    for c in node.get("children") or []:
        m += _mounts(c)
    return m


def _paths(node):
    return [node.get("path")] + [p for c in node.get("children") or [] for p in _paths(c)]


def _vols(node):
    out = []
    for c in node.get("children") or []:
        mp = [x for x in (c.get("mountpoints") or [c.get("mountpoint")]) if x]
        if c.get("fstype") and c["fstype"] not in ("crypto_LUKS", "LVM2_member", "swap"):
            out.append((c.get("label") or c["name"]) + (f" ({mp[0]})" if mp else ""))
        out += _vols(c)
    return out


def list_disks():
    cols = _COLS
    r = subprocess.run(["lsblk", "-J", "-b", "-o", cols], capture_output=True)
    if r.returncode:  # ältere util-linux kennt MOUNTPOINTS noch nicht
        r = subprocess.run(["lsblk", "-J", "-b", "-o", cols.replace("MOUNTPOINTS", "MOUNTPOINT")], capture_output=True)
    src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "-T", SCRIPT], capture_output=True, text=True).stdout.strip().split("[")[0]
    out = []
    for d in json.loads(r.stdout.decode("utf-8", "replace"))["blockdevices"]:
        size = int(d.get("size") or 0)
        if d.get("type") != "disk" or d["name"].startswith(("zram", "ram", "nbd")) or not size:
            continue
        sysm = _SYSTEM & set(_mounts(d))
        tran, rota = (d.get("tran") or "").lower(), str(d.get("rota")).lower() in ("1", "true")
        kind = "NVMe" if tran == "nvme" else "HDD" if rota else "Flash" if tran == "usb" or str(d.get("rm")).lower() in ("1", "true") else "SSD"
        locked = (f"Systemplatte ({sorted(sysm)[0]})" if sysm else "Enthält dieses Programm" if src and src in _paths(d) else "")
        out.append(dict(id=d["name"], path=d["path"], size=size, model=(d.get("model") or "").strip(), serial=(d.get("serial") or "").strip(),
                        bus=tran.upper(), kind=kind, volumes=_vols(d), locked=locked, node=d))
    return out


def preflight(disk):
    """Nicht zerstörend: Öffnen zum Schreiben scheitert ohne Root-Rechte oder bei schreibgeschütztem Gerät."""
    os.close(os.open(disk["path"], os.O_RDWR))


def prepare(disk):
    for mp in dict.fromkeys(_mounts(disk["node"])):
        r = subprocess.run(["umount", "--", mp], capture_output=True, text=True)
        if r.returncode:
            raise WipeError(f"{mp} konnte nicht ausgehängt werden: {r.stderr.strip()}")


def _tool(*names):
    for n in names:
        p = shutil.which(n, path=os.environ.get("PATH", "") + ":/sbin:/usr/sbin")
        if p:
            return p
    return None


def setup_fs(d):
    out = ["ext4"] if _tool("mkfs.ext4") else []
    if _tool("mkfs.exfat", "mkexfatfs"):
        out.append("exfat")
    if _tool("mkfs.ntfs", "mkntfs"):
        out.append("ntfs")
    if _tool("mkfs.vfat", "mkfs.fat") and d["size"] <= FAT32_MAX:
        out.append("fat32")
    return out or ["ext4"]


def native_fs(d):
    return "ext4"


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise WipeError(f"{os.path.basename(cmd[0])} fehlgeschlagen: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def _partition(dev):
    for _ in range(20):  # der Kernel meldet die neue Partition mit kurzer Verzögerung
        subprocess.run(["udevadm", "settle", "--timeout=5"], capture_output=True)
        out = subprocess.run(["lsblk", "-nrpo", "PATH,TYPE", dev], capture_output=True, text=True).stdout
        parts = [ln.split()[0] for ln in out.splitlines() if ln.endswith(" part")]
        if parts:
            return parts[0]
        subprocess.run(["partprobe", dev], capture_output=True)
        time.sleep(0.5)
    raise WipeError("Die neue Partition wurde vom System nicht erkannt.")


def setup_disk(disk, scheme, fs, label, owner):
    """Partitionstabelle (sfdisk), eine Partition über den ganzen Datenträger, Dateisystem anlegen."""
    dev = disk["path"]
    gpt = "0FC63DAF-8483-4772-8E79-3D69D8477DE4" if fs == "ext4" else "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"  # Linux bzw. Microsoft Basic Data
    dos = "83" if fs == "ext4" else "c" if fs == "fat32" else "7"
    subprocess.run([_tool("wipefs") or "wipefs", "-a", dev], capture_output=True)
    _run(["sfdisk", "--quiet", "--wipe", "always", "--wipe-partitions", "always", dev],
         input=(f"label: gpt\nstart=2048, type={gpt}\n" if scheme == "gpt" else f"label: dos\nstart=2048, type={dos}\n"))
    part = _partition(dev)
    if fs == "ext4":
        cmd = [_tool("mkfs.ext4"), "-F", "-q", "-L", label] + (["-E", f"root_owner={owner[0]}:{owner[1]}"] if owner[0] is not None else []) + [part]
    elif fs == "exfat":
        cmd = [_tool("mkfs.exfat"), "-L", label, part] if _tool("mkfs.exfat") else [_tool("mkexfatfs"), "-n", label, part]
    elif fs == "ntfs":
        cmd = [_tool("mkfs.ntfs", "mkntfs"), "-f", "-q", "-L", label, part]
    else:
        cmd = [_tool("mkfs.vfat", "mkfs.fat"), "-F", "32", "-n", label, part]
    _run(cmd)
    subprocess.run(["udevadm", "settle", "--timeout=10"], capture_output=True)
    return part


def after_setup(info):
    """Als normaler Benutzer einhängen, damit das neue Laufwerk im Dateimanager erscheint (meist hängt der Desktop es selbst ein)."""
    try:
        r = subprocess.run(["udisksctl", "mount", "-b", info], capture_output=True, text=True, timeout=20)
        m = re.search(r" at (/[^`'\n]+?)\.?\s*$", r.stdout.strip()) or re.search(r"already mounted at [`']([^`']+)'", r.stderr)
        return m.group(1) if m else info
    except (OSError, subprocess.SubprocessError):
        return info


class LinuxDev(PosixDev):
    def finish(self):
        try:
            import fcntl
            fcntl.ioctl(self.fd, 0x125F)  # BLKRRPART: Kernel soll die (jetzt leere) Partitionstabelle neu lesen
        except OSError:
            pass


def open_dev(path):
    """O_DIRECT umgeht den Seitencache, O_EXCL schlägt fehl, solange das Gerät noch verwendet wird."""
    try:
        return retry_busy(lambda: LinuxDev(path, os.O_DIRECT | os.O_EXCL))
    except OSError as e:
        if e.errno != errno.EINVAL:
            raise
        return retry_busy(lambda: LinuxDev(path, os.O_EXCL))


def launch_worker(job):
    cmd = [sys.executable, os.path.join(os.path.dirname(job), "worker.py"), "--worker", job]
    if os.geteuid() != 0:
        if not shutil.which("pkexec"):
            raise WipeError("pkexec (PolicyKit) fehlt. Bitte installieren oder Schredder mit sudo starten.")
        cmd = ["pkexec"] + cmd
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def inhibit_sleep():
    try:
        subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=Schredder", "--why=Datenträger wird gelöscht",
                          "tail", f"--pid={os.getpid()}", "-f", "/dev/null"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
