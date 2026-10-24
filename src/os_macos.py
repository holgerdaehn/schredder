
# ───────────────────────── macOS ─────────────────────────
import plistlib, shlex

PLATFORM = "macos"
PREPARE_DESTROYS = False  # Aushängen verändert keine Daten
_BUS = {"PCI-Express": "PCIe", "Apple Fabric": "Apple"}


def _plist(*cmd):
    try:
        return plistlib.loads(subprocess.run(cmd, capture_output=True).stdout)
    except Exception:
        return {}


def _whole(dev):
    """Physischer Datenträger (diskN) hinter einem Gerät oder Mountpoint, auch bei APFS-Containern."""
    i = _plist("diskutil", "info", "-plist", dev)
    st = i.get("APFSPhysicalStores")
    m = re.match(r"disk\d+", (st[0].get("APFSPhysicalStore") if st else i.get("ParentWholeDisk")) or "")
    return m.group(0) if m else None


def _containers(full, disk):
    return [c for c in full if any(s.get("DeviceIdentifier", "").startswith(disk + "s") for s in c.get("APFSPhysicalStores", []))]


def list_disks():
    phys = _plist("diskutil", "list", "-plist", "physical").get("AllDisksAndPartitions", [])
    full = _plist("diskutil", "list", "-plist").get("AllDisksAndPartitions", [])
    df = subprocess.run(["df", "-P", SCRIPT], capture_output=True, text=True).stdout.splitlines()
    boot, here = _whole("/"), _whole(df[-1].split()[0]) if len(df) > 1 else None
    out = []
    for e in phys:
        i = e["DeviceIdentifier"]
        info = _plist("diskutil", "info", "-plist", i)
        vols = [p["VolumeName"] for p in e.get("Partitions", []) if p.get("VolumeName")]
        for c in _containers(full, i):
            vols += [v["VolumeName"] for v in c.get("APFSVolumes", []) if v.get("VolumeName")]
        bus, solid = info.get("BusProtocol") or "", info.get("SolidState")
        kind = ("NVMe" if bus in _BUS else "SSD" if bus != "USB" else "Flash") if solid else (
            "HDD" if solid is False else "Flash" if info.get("RemovableMedia") else "?")
        locked = ("Systemplatte (enthält das Startvolume)" if i == boot else "Interner Datenträger" if info.get("Internal") else
                  "Enthält dieses Programm" if i == here else "")
        out.append(dict(id=i, path="/dev/r" + i, size=int(e["Size"]), model=(info.get("MediaName") or "").strip(), serial="",
                        bus=_BUS.get(bus, bus), kind=kind, volumes=list(dict.fromkeys(vols)), locked=locked))
    return out


def preflight(disk):
    """Nicht zerstörend: Lesezugriff aufs Rohgerät scheitert ohne Root-Rechte bzw. ohne Festplattenzugriff (Datenschutz & Sicherheit)."""
    os.close(os.open(disk["path"], os.O_RDONLY))


def prepare(disk):
    """Alle Volumes (auch APFS-Container) aushängen, sonst verweigert macOS den Schreibzugriff."""
    full = _plist("diskutil", "list", "-plist").get("AllDisksAndPartitions", [])
    for x in [disk["id"]] + [c["DeviceIdentifier"] for c in _containers(full, disk["id"])]:
        r = subprocess.run(["diskutil", "unmountDisk", "force", "/dev/" + x], capture_output=True, text=True)
        if r.returncode:
            raise WipeError(f"{x} konnte nicht ausgehängt werden: {(r.stderr or r.stdout).strip()}")


def setup_fs(d):
    return ["apfs", "hfs", "exfat"] + (["fat32"] if d["size"] <= FAT32_MAX else [])


def native_fs(d):
    return "hfs" if d["kind"] == "HDD" else "apfs"  # APFS ist für Flash gebaut, Mac OS Extended für mechanische Platten


def setup_disk(disk, scheme, fs, label, owner):
    """Partitionstabelle + Dateisystem in einem Schritt; macOS hängt das neue Volume selbst ein."""
    name = {"apfs": "APFS", "hfs": "JHFS+", "exfat": "ExFAT", "fat32": "FAT32"}[fs]
    r = subprocess.run(["diskutil", "eraseDisk", name, label, scheme.upper(), "/dev/" + disk["id"]], capture_output=True, text=True)
    if r.returncode:
        raise WipeError("Einrichten fehlgeschlagen: " + (r.stderr or r.stdout).strip()[-300:])
    full = _plist("diskutil", "list", "-plist").get("AllDisksAndPartitions", [])
    mine = [e for e in full if e["DeviceIdentifier"] == disk["id"]] + _containers(full, disk["id"])
    mps = [p["MountPoint"] for e in mine for p in e.get("Partitions", []) + e.get("APFSVolumes", []) if p.get("MountPoint")]
    return ", ".join(mps) or "eingerichtet"


def after_setup(info):
    return info  # macOS hängt das Volume selbst ein


def open_dev(path):
    return retry_busy(lambda: PosixDev(path))  # /dev/rdiskN: Rohgerät, kein Dateicache


def launch_worker(job):
    cmd = [sys.executable, os.path.join(os.path.dirname(job), "worker.py"), "--worker", job]
    if os.geteuid() == 0:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sh = " ".join(shlex.quote(c) for c in cmd).replace("\\", "\\\\").replace('"', '\\"')
    return subprocess.Popen(["osascript", "-e", f'do shell script "{sh}" with administrator privileges'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def inhibit_sleep():
    try:
        subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])  # endet automatisch mit dem Worker
    except OSError:
        pass
