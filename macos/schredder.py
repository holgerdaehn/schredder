# GENERIERT von build.py aus src/ – Änderungen bitte dort machen.
# -*- coding: utf-8 -*-
"""Schredder – Datenträger sicher und schnell löschen.

Aufbau: Die Oberfläche ist eine lokale Webseite (nur Python-Standardbibliothek, kein Tk) und läuft ohne
Administratorrechte. Erst beim Start eines Löschvorgangs wird dieselbe Datei erneut mit `--worker` und erhöhten
Rechten gestartet. Der Worker prüft zuerst die Rechte und den Schreibzugriff (ohne etwas zu verändern), bearbeitet
dann einen oder mehrere Datenträger (gleichzeitig oder nacheinander), bietet danach die Einrichtung als Laufwerk
an und meldet den Fortschritt über eine Statusdatei.
"""
import ctypes, errno, json, mmap, os, platform, random, re, shutil, subprocess, sys, tempfile, threading, time, traceback, types
from collections import deque
from datetime import datetime

VERSION = "1.0.1"
SCRIPT = os.path.abspath(__file__)
CHUNK = 4 << 20        # 4 MiB pro Schreib-/Lesevorgang (Vielfaches jeder Sektorgröße)
POOL = 64 << 20        # Zufallspool: einmal vom Betriebssystem, dann laufend erneuert

MODES = [
    dict(name="Schnell", sub="1 Durchgang", passes=["zero"],
         info="Nullen – genügt für Weitergabe und Wiederverwendung (NIST „Clear“)."),
    dict(name="Sicher", sub="2 Durchgänge", passes=["random", "zero"],
         info="Zufallsdaten, dann Nullen – empfohlen vor Verkauf oder Entsorgung."),
    dict(name="Gründlich", sub="4 Durchgänge", passes=["random", "random", "random", "zero"],
         info="3× Zufallsdaten, dann Nullen – höchste Sicherheit, dauert ca. 4× so lange."),
]
PASS_NAME = {"zero": "Nullen", "random": "Zufallsdaten"}
MBR_MAX = 2 * 1024 ** 4      # MBR adressiert höchstens 2 TiB
FAT32_MAX = 32 * 1024 ** 3   # größere FAT32-Volumes erzeugt weder der Windows-Formatierer noch die SD-Norm
ALIVE_POLLS = 1500  # Durchläufe à 0,4 s ohne Lebenszeichen der Oberfläche (ca. 10 Minuten), dann gibt der Worker auf
SETUP_WAIT = 600             # Frist für die Antwort zur Einrichtung, wenn keine alive-Datei vorhanden ist (Sekunden)
SCHEME_NAME = {"gpt": "GPT", "mbr": "MBR"}
SCHEME_WHY = {"gpt": "GPT – Standard moderner Systeme", "mbr": "MBR – läuft auch an älteren Geräten (Fernseher, Auto, Kamera)"}
FS_NAME = {"ntfs": "NTFS", "exfat": "exFAT", "fat32": "FAT32", "ext4": "ext4", "apfs": "APFS", "hfs": "Mac OS Extended"}
FS_WHY = {"fat32": "FAT32 – läuft praktisch überall (höchstens 4 GB je Datei)",
          "exfat": "exFAT – Windows, macOS und Linux, keine 4-GB-Dateigrenze",
          "ntfs": "NTFS – Standard für Windows-Datenträger (Zugriffsrechte, große Dateien)",
          "apfs": "APFS – Standard für SSDs unter macOS", "hfs": "Mac OS Extended – empfohlen für mechanische Festplatten unter macOS",
          "ext4": "ext4 – Standard unter Linux"}
FLOW = [("Gleichzeitig", True), ("Nacheinander", False)]
PERF = [("Automatisch (empfohlen)", 0), ("1 Thread", 1), ("2 Threads", 2), ("4 Threads", 4), ("8 Threads", 8), ("16 Threads", 16)]

_libc = ctypes.cdll.msvcrt if os.name == "nt" else ctypes.CDLL(None)
_libc.memcmp.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
_libc.memcmp.restype = ctypes.c_int


# ───────────────────────── Hilfsfunktionen ─────────────────────────

def human(n, dec=1):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or u == "TB":
            return (f"{n:.0f} {u}" if u == "B" else f"{n:.{dec}f} {u}").replace(".", ",")
        n /= 1000


def fmt_time(s):
    h, r = divmod(int(max(s, 0)), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def auto_threads(d):
    """Parallelität je Medium: NVMe braucht viele gleichzeitige Anfragen, HDD/USB-Speicher wenige."""
    return {"NVMe": 8, "SSD": 4}.get(d["kind"], 2)


def clean_label(fs, label):
    """Name des neuen Laufwerks: nur zulässige Zeichen, Länge je Dateisystem."""
    limit = {"fat32": 11, "exfat": 11, "ntfs": 32, "ext4": 16, "apfs": 27, "hfs": 27}[fs]
    label = re.sub(r"[^A-Z0-9 _-]", "", label.upper()) if fs == "fat32" else "".join(c for c in label if c.isalnum() or c in " _-")
    return label.strip().lstrip("-")[:limit].strip() or ("NEU" if fs == "fat32" else "Neu")


def format_why(d, scheme, fs):
    """Begründung für eine Kombination aus Partitionstabelle und Dateisystem."""
    sch = "GPT – nötig für Datenträger über 2 TB" if scheme == "gpt" and d["size"] > MBR_MAX else SCHEME_WHY[scheme]
    why = f"{sch} · {FS_WHY[fs]}"
    if d["kind"] != "Flash" and d["bus"] == "USB" and fs != "exfat":
        why += " (für den Austausch mit anderen Systemen exFAT wählen)"
    return why


def recommend(d):
    """Bestes Format für einen frisch gelöschten Datenträger: (Partitionstabelle, Dateisystem, Begründung).
    USB-Sticks/SD-Karten: MBR (läuft überall), bis 32 GB FAT32, darüber exFAT. Festplatten/SSDs: GPT mit dem Standard des Systems.
    Über 2 TB geht nur GPT."""
    size, opts, flash = d["size"], setup_fs(d), d["kind"] == "Flash"
    want = ("fat32" if size <= FAT32_MAX else "exfat") if flash else native_fs(d)
    fs = next((f for f in (want, "exfat", "ntfs", "fat32", "ext4") if f in opts), opts[0])
    scheme = "mbr" if flash and size <= MBR_MAX and fs != "apfs" else "gpt"
    return scheme, fs, format_why(d, scheme, fs)


_HINT_POSIX = {
    errno.EPERM: "Zugriff vom System blockiert", errno.EACCES: "Zugriff verweigert",
    errno.EBUSY: "Datenträger wird noch verwendet (eingehängt, LVM/RAID, verschlüsselt?)",
    errno.EIO: "Ein-/Ausgabefehler – Datenträger defekt oder Kabel locker?",
    errno.ENXIO: "Datenträger nicht mehr vorhanden (getrennt?)", errno.ENODEV: "Datenträger nicht mehr vorhanden (getrennt?)",
    errno.EROFS: "Datenträger ist schreibgeschützt",
}
_HINT_WIN = {
    5: "Zugriff verweigert – Datenträger wird noch von einem Programm verwendet", 19: "Datenträger ist schreibgeschützt",
    21: "Datenträger nicht bereit", 23: "Datenfehler (CRC) – Datenträger defekt?", 32: "Datenträger wird von einem Programm verwendet",
    1117: "Ein-/Ausgabefehler – Datenträger defekt oder Kabel locker?", 1167: "Datenträger nicht mehr verbunden",
}


def explain(e):
    w = getattr(e, "winerror", None)
    hint = _HINT_WIN.get(w) if w else _HINT_POSIX.get(getattr(e, "errno", None))
    if hint and PLATFORM == "macos" and getattr(e, "errno", None) == errno.EPERM:
        hint += " (Systemeinstellungen → Datenschutz & Sicherheit → Festplattenzugriff für das Terminal/Python erlauben)"
    return f"{hint}: {e}" if hint else str(e)


class Cancelled(Exception):
    pass


class WipeError(Exception):
    pass


# ───────────────────────── Engine ─────────────────────────

class Buf:
    """Seitenausgerichteter Puffer – Voraussetzung für ungepufferte E/A (O_DIRECT, NO_BUFFERING)."""

    def __init__(self, size):
        self.mm = mmap.mmap(-1, size)
        self._c = ctypes.c_char.from_buffer(self.mm)
        self.addr = ctypes.addressof(self._c)
        self.mv = memoryview(self.mm)


class Pool:
    """Zufallsdaten ohne Engpass: Pool aus dem OS-Zufallsgenerator, jeder Block startet an einem
    zufälligen Byte-Versatz; ein Teil des Pools wird laufend mit frischer Entropie ersetzt."""
    REFRESH = 1 << 20

    def __init__(self):
        self.buf = Buf(POOL)
        for o in range(0, POOL, 8 << 20):
            self.buf.mm[o:o + (8 << 20)] = os.urandom(8 << 20)

    def fill(self, dst, n, rnd):
        ctypes.memmove(dst, self.buf.addr + rnd.randrange(POOL - n + 1), n)

    def refresh(self, rnd):
        o = rnd.randrange(POOL - self.REFRESH)
        self.buf.mm[o:o + self.REFRESH] = os.urandom(self.REFRESH)


class Counter:
    def __init__(self):
        self.value, self.lock = 0, threading.Lock()

    def add(self, n):
        with self.lock:
            self.value += n


def retry_busy(fn, tries=10):
    """Direkt nach dem Aushängen halten Systemdienste das Gerät kurz fest (EBUSY)."""
    for i in range(tries):
        try:
            return fn()
        except OSError as e:
            if e.errno != errno.EBUSY or i == tries - 1:
                raise
            time.sleep(0.5)


def run_chunks(size, nthreads, make_ctx, work, cancel):
    """Verteilt die Blöcke 0,1,2,… reihum an die Threads: viele Anfragen gleichzeitig (SSD/NVMe),
    aber nahezu sequentielle Zugriffe (HDD ohne Kopfsprünge)."""
    lock, st = threading.Lock(), {"i": 0, "err": None}

    def loop():
        try:
            ctx = make_ctx()
            while st["err"] is None and not cancel():
                with lock:
                    off = st["i"] * CHUNK
                    st["i"] += 1
                if off >= size:
                    return
                try:
                    work(ctx, off, min(CHUNK, size - off))
                except OSError as e:
                    raise WipeError(f"Fehler bei Position {human(off)}: {explain(e)}") from e
        except BaseException as e:
            if st["err"] is None:
                st["err"] = e

    ts = [threading.Thread(target=loop) for _ in range(nthreads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if st["err"] is not None:
        raise st["err"]
    if cancel():
        raise Cancelled()


def write_pass(dev, size, kind, nthreads, pool, cnt, cancel):
    def make_ctx():
        return types.SimpleNamespace(buf=Buf(CHUNK), k=0, rnd=random.Random(int.from_bytes(os.urandom(8), "little")))

    def work(c, off, n):
        if kind == "random":
            pool.fill(c.buf.addr, n, c.rnd)
            c.k += 1
            if c.k % 8 == 0:
                pool.refresh(c.rnd)
        dev.write(c.buf, n, off)
        cnt.add(n)

    run_chunks(size, nthreads, make_ctx, work, cancel)
    dev.flush()


def verify_pass(dev, size, nthreads, cnt, cancel):
    zero = Buf(CHUNK)

    def work(c, off, n):
        dev.read(c.buf, n, off)
        if _libc.memcmp(c.buf.addr, zero.addr, n):
            raise WipeError(f"Prüfung fehlgeschlagen: Bei Position {human(off)} (Byte {off}) stehen noch Daten.")
        cnt.add(n)

    run_chunks(size, nthreads, lambda: types.SimpleNamespace(buf=Buf(CHUNK)), work, cancel)


class Status:
    """Fortschritt Worker → GUI: JSON-Datei, atomar ersetzt. Ein Eintrag je Datenträger."""

    def __init__(self, path, ids):
        self.path, self.lock = path, threading.Lock()
        self.d = {"state": "start", "disks": {i: {"state": "wait", "done": 0, "total": 0} for i in ids}}

    def _flush(self, tries):
        self.d["t"] = time.time()
        tmp = self.path + ".tmp"
        for _ in range(tries):
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.d, f)
                os.replace(tmp, self.path)  # unter Windows kurz blockiert, solange die GUI liest
                return
            except OSError:
                time.sleep(0.05)

    def set(self, tries=1, **kw):
        with self.lock:
            self.d.update(kw)
            self._flush(tries)

    def disk(self, i, tries=1, **kw):
        with self.lock:
            self.d["disks"][i].update(kw)
            self._flush(tries)

    def tick(self, cnt):
        with self.lock:
            for i, c in cnt.items():
                self.d["disks"][i]["done"] = c.value
            self._flush(1)


def explain_any(e):
    return explain(e) if isinstance(e, OSError) else str(e) if isinstance(e, WipeError) else f"{type(e).__name__}: {e}"


def data_note(ds):
    """Was ist mit den Daten passiert? (für Fehler- und Abbruchmeldungen)"""
    at = ds.get("at")
    if at == "verify":
        return "Der Datenträger wurde vollständig überschrieben; nur die Prüfung ist nicht abgeschlossen."
    if at == "write" and ds.get("done"):
        return "Der Datenträger ist nur teilweise überschrieben."
    if ds.get("modified"):
        return "Die Partitionen wurden bereits entfernt, die Daten sind aber noch nicht überschrieben – bitte den Löschvorgang erneut starten."
    return "Es wurde nichts verändert."


def require_rights():
    """Ohne Administrator-/Root-Rechte wird gar nichts angefasst."""
    if not is_admin():
        raise WipeError("Schredder läuft ohne Administrator-/Root-Rechte.")


def find_disks(job):
    """Prüft die Wahl der GUI erneut (getauscht? gesperrt?) – je Datenträger, ein Problem stoppt die anderen nicht."""
    cur = {d["id"]: d for d in list_disks()}
    ok, bad = {}, {}
    for j in job["disks"]:
        d = cur.get(j["id"])
        if d is None:
            bad[j["id"]] = "Datenträger nicht mehr gefunden."
        elif (d["path"], d["size"], d["model"], d["serial"]) != (j["path"], j["size"], j["model"], j["serial"]):
            bad[j["id"]] = "Der Datenträger hat sich seit der Auswahl geändert. Bitte neu auswählen."
        elif d["locked"]:
            bad[j["id"]] = "Datenträger ist gesperrt: " + d["locked"]
        else:
            ok[j["id"]] = d
    return ok, bad


def run_disk(job, disk, pool, cnt, st, cancel, par):
    """Ein Datenträger: erst nicht zerstörende Prüfungen, dann (ggf.) Vorbereitung, dann Schreiben und Prüfen."""
    i, size, passes = disk["id"], disk["size"], job["passes"]
    verify = bool(job["verify"]) and passes[-1] == "zero"
    nthreads = job["threads"] or min(auto_threads(disk), max(2, 32 // par))
    t0, phase = time.time(), "prepare"
    try:
        if cancel():
            raise Cancelled()
        st.disk(i, state="prepare", threads=nthreads)
        preflight(disk)  # Rechte, Schreibzugriff, nicht gesperrt – verändert nichts
        if cancel():
            raise Cancelled()
        prepare(disk)    # ab hier kann der Datenträger verändert sein (Windows: Partitionen entfernt)
        st.disk(i, modified=PREPARE_DESTROYS)
        dev = open_dev(disk["path"])
        try:
            phase = "write"
            for k, kind in enumerate(passes):
                st.disk(i, state="write", kind=kind, n_pass=k + 1, n_passes=len(passes))
                write_pass(dev, size, kind, nthreads, pool, cnt, cancel)
            if verify:
                phase = "verify"
                st.disk(i, state="verify")
                verify_pass(dev, size, nthreads, cnt, cancel)
            getattr(dev, "finish", lambda: None)()
        finally:
            dev.close()
        st.disk(i, tries=40, state="done", seconds=time.time() - t0, verified=verify, passes=len(passes))
    except Cancelled:
        st.disk(i, tries=40, state="cancelled", at=phase, seconds=time.time() - t0)
    except BaseException as e:
        st.disk(i, tries=40, state="error", at=phase, error=explain_any(e), detail=traceback.format_exc(), seconds=time.time() - t0)


def run_all(job, disks, st, cancel):
    """Alle gültigen Datenträger: gleichzeitig (ein Thread je Datenträger) oder nacheinander."""
    if not disks:
        return
    passes = job["passes"]
    verify = bool(job["verify"]) and passes[-1] == "zero"
    ids = list(disks)
    par = len(ids) if job.get("parallel", True) else 1
    cnt = {i: Counter() for i in ids}
    for i in ids:
        st.disk(i, total=disks[i]["size"] * (len(passes) + verify), done=0, modified=False)
    st.set(state="running")
    inhibit_sleep()
    pool = Pool() if "random" in passes else None
    todo, lock, stop = list(ids), threading.Lock(), threading.Event()

    def runner():
        while True:
            with lock:
                if not todo:
                    return
                i = todo.pop(0)
            run_disk(job, disks[i], pool, cnt[i], st, cancel, par)

    def report():
        while not stop.wait(0.25):
            st.tick(cnt)
    rep = threading.Thread(target=report, daemon=True)
    rep.start()
    ts = [threading.Thread(target=runner) for _ in range(min(par, len(ids)))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    rep.join()
    st.tick(cnt)


def wait_decision(job, cancel):
    """Wartet auf die Antwort der Oberfläche (Datei setup.json). Nach einem langen Lauf kann es Stunden dauern, bis jemand
    antwortet: Der Worker wartet daher, solange die Oberfläche lebt (sie berührt laufend die Datei alive). None bei Abbruch
    oder wenn die Oberfläche weg ist; ohne alive-Datei gilt die feste Frist SETUP_WAIT."""
    end, alive, last, quiet = time.time() + SETUP_WAIT, job.get("alive_file"), None, 0
    while not cancel():
        try:
            with open(job["setup_file"], encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        if alive:
            try:
                mark = os.stat(alive).st_mtime_ns
            except OSError:
                mark = None
            quiet, last = (0 if mark != last else quiet + 1), mark  # gezählt wird je Durchlauf, nicht nach Uhrzeit (Ruhezustand!)
            if quiet > ALIVE_POLLS:
                return None
        elif time.time() > end:
            return None
        time.sleep(0.4)
    return None


def apply_setup(job, disks, decision, st):
    """Eingerichtet wird nur, was bestätigt wurde – und nur für erfolgreich gelöschte Datenträger mit erlaubten Werten."""
    owner = (job.get("uid"), job.get("gid"))
    for it in decision.get("setup", []):
        i, d = it.get("id"), disks.get(it.get("id"))
        if d is None or st.d["disks"][i].get("state") != "done":
            continue
        scheme, fs = it.get("scheme"), it.get("fs")
        if scheme not in SCHEME_NAME or fs not in setup_fs(d) or (scheme == "mbr" and (d["size"] > MBR_MAX or fs == "apfs")):
            st.disk(i, tries=40, setup="error", setup_error="Ungültige Angaben für die Einrichtung.")
            continue
        label = clean_label(fs, str(it.get("label") or ""))
        st.disk(i, tries=40, setup="run", setup_scheme=scheme, setup_fs=fs, setup_label=label)
        try:
            st.disk(i, tries=40, setup="done", setup_info=setup_disk(d, scheme, fs, label, owner))
        except BaseException as e:
            st.disk(i, tries=40, setup="error", setup_error=explain_any(e))


def worker(jobfile):
    global SCRIPT
    with open(jobfile, encoding="utf-8") as f:
        job = json.load(f)
    SCRIPT = job.get("script", SCRIPT)  # der Worker läuft aus einer Kopie; „Enthält dieses Programm“ gilt für das Original
    st = Status(job["status"], [d["id"] for d in job["disks"]])
    st.set()
    cancel = lambda: os.path.exists(job["cancel"])
    try:
        require_rights()  # allererster Schritt: ohne Rechte passiert nichts
        ok, bad = find_disks(job)
        for i, msg in bad.items():
            st.disk(i, state="error", error=msg, modified=False)
        run_all(job, ok, st, cancel)
        wiped = [i for i in ok if st.d["disks"][i].get("state") == "done"]
        decision = None
        if wiped and not cancel():
            if "setup_plan" in job:  # vor dem Löschen festgelegt: nichts mehr abwarten
                decision = {"setup": job["setup_plan"]}
            elif job.get("setup_file"):  # gelöschte Datenträger sind leer und unsichtbar: Einrichtung anbieten
                st.set(tries=40, state="ask", ask=wiped)
                decision = wait_decision(job, cancel)
        if decision and decision.get("setup"):
            st.set(tries=40, state="setup")
            apply_setup(job, ok, decision, st)
        st.set(tries=40, state="cancelled" if cancel() else "done")
        return 0
    except BaseException as e:
        st.set(tries=40, state="error", error=explain_any(e), detail=traceback.format_exc())
        return 1


def to_text(b):
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")


# ───────────────────────── POSIX-Gerätezugriff (macOS, Linux) ─────────────────────────

def is_admin():
    return os.geteuid() == 0


class PosixDev:
    """Ein Dateideskriptor für alle Threads: pwrite/preadv tragen den Offset selbst und geben die GIL frei."""

    def __init__(self, path, flags=0):
        self.fd = os.open(path, os.O_RDWR | flags)

    def write(self, buf, n, off):
        done = 0
        while done < n:
            w = os.pwrite(self.fd, buf.mv[done:n], off + done)
            if w <= 0:
                raise OSError(errno.EIO, "Schreiben lieferte 0 Bytes")
            done += w

    def read(self, buf, n, off):
        done = 0
        while done < n:
            r = os.preadv(self.fd, [buf.mv[done:n]], off + done)
            if r <= 0:
                raise OSError(errno.EIO, "Unerwartetes Ende des Datenträgers")
            done += r

    def flush(self):
        try:
            import fcntl
            if hasattr(fcntl, "F_FULLFSYNC"):  # macOS: bis auf das Medium durchschreiben
                return fcntl.fcntl(self.fd, fcntl.F_FULLFSYNC)
        except OSError:
            pass
        os.fsync(self.fd)

    def close(self):
        os.close(self.fd)


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


# ───────────────────────── Weboberfläche (lokaler Server) ─────────────────────────
import hmac
import http.server
import secrets
import webbrowser

TITLE = "Schredder – Datenträger sicher löschen"
FINAL = ("done", "cancelled", "error")
SETUP_TOOL = {"win": "der Datenträgerverwaltung", "macos": "dem Festplattendienstprogramm", "linux": "„Laufwerke“ (GNOME Disks)"}
IDLE_EXIT = 180  # Sekunden ohne Lebenszeichen der Seite, danach beendet sich das Programm (nie während eines Löschvorgangs)
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def say(msg):
    try:
        print(msg, flush=True)
    except Exception:  # pythonw hat keine Konsole
        pass


def disk_info(d):
    return dict(id=d["id"], model=d["model"], serial=d["serial"], size=d["size"], size_h=human(d["size"]), bus=d["bus"], kind=d["kind"],
                volumes=d["volumes"], locked=d["locked"], path=d["path"], setup=ask_info(d))


def ask_info(d):
    """Alles, was der Einrichtungsdialog für einen gelöschten Datenträger braucht (Empfehlung, Auswahl, Begründungen)."""
    scheme0, fs0, _ = recommend(d)
    fsopts = setup_fs(d)
    schemes = {k: ["gpt"] if d["size"] > MBR_MAX or k == "apfs" else ["gpt", "mbr"] for k in fsopts}
    return dict(id=d["id"], model=d["model"] or "Datenträger", size_h=human(d["size"]), rec=dict(scheme=scheme0, fs=fs0),
                label=clean_label(fs0, "Neu"), fs=[dict(key=k, name=FS_NAME[k]) for k in fsopts], schemes=schemes,
                why={f"{sc}|{k}": format_why(d, sc, k) for k in fsopts for sc in schemes[k]})


class Run:
    """Ein Löschauftrag: startet den Worker, liest dessen Status und bereitet ihn für die Oberfläche auf."""

    def __init__(self, ds, mode, verify, threads, par, plan=None):
        self.ds, self.mode, self.par = ds, mode, par
        self.tmp = tempfile.mkdtemp(prefix="schredder-")
        self.f_status, self.f_cancel, self.f_setup, self.f_alive = (os.path.join(self.tmp, n) for n in ("status.json", "cancel", "setup.json", "alive"))
        open(self.f_alive, "w").close()
        shutil.copy2(SCRIPT, os.path.join(self.tmp, "worker.py"))  # Worker läuft aus privater Kopie (kein Zugriffsproblem in Downloads/Desktop)
        job = dict(script=SCRIPT, disks=[{k: d[k] for k in ("id", "path", "size", "model", "serial")} for d in ds], passes=mode["passes"],
                   verify=verify, threads=threads, parallel=par, status=self.f_status, cancel=self.f_cancel, setup_file=self.f_setup, alive_file=self.f_alive,
                   uid=os.getuid() if hasattr(os, "getuid") else None, gid=os.getgid() if hasattr(os, "getgid") else None)
        if plan is not None:  # Einrichtung wurde vor dem Löschen festgelegt (leer = nicht einrichten)
            job["setup_plan"] = plan
        jf = os.path.join(self.tmp, "job.json")
        with open(jf, "w", encoding="utf-8") as f:
            json.dump(job, f)
        try:
            self.proc = launch_worker(jf)
        except Exception:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise
        self.t0, self.t_io, self.answered, self.cancelling, self.done, self.report = time.time(), None, False, False, False, ""
        self.samples, self.dsamples, self.hist, self.tick, self.skip = deque(), {d["id"]: deque() for d in ds}, [], 0, 1
        self.stats = dict(speed="–", avg="–", el="0:00:00", eta="–", done="–")
        self.title = (f"Löscht {ds[0]['id']} – {ds[0]['model'] or 'Datenträger'} ({human(ds[0]['size'])})" if len(ds) == 1
                      else f"Löscht {len(ds)} Datenträger ({human(sum(d['size'] for d in ds))})")
        self.view = self.base("launching", "Administratorrechte werden angefordert …",
                              [self.row(d, 0, "acc", "Wartet auf Administratorrechte …", "", "") for d in ds])
        threading.Thread(target=self._loop, daemon=True).start()

    # ── Ansicht ──
    def row(self, d, frac, col, phase, rate, pct):
        return dict(id=d["id"], sub=f"{d['model'] or 'Datenträger'} · {human(d['size'])}", frac=frac, col=col, phase=phase, rate=rate, pct=pct)

    def base(self, state, phase, rows, frac=0.0, col="acc"):
        return dict(state=state, title=self.title, phase=phase, frac=frac, pct=f"{frac * 100:.1f} %" if frac else "", col=col, rows=rows,
                    multi=len(self.ds) > 1, stats=dict(self.stats), hist=list(self.hist), can_cancel=not self.cancelling and state in ("launching", "running"))

    def _loop(self):
        while not self.done:
            try:
                os.utime(self.f_alive)  # Lebenszeichen: der Worker wartet auf die Antwort zur Einrichtung, solange es kommt
            except OSError:
                pass
            try:
                self.poll()
            except Exception as e:  # die Oberfläche darf nie an einem Darstellungsfehler hängen
                self.view = dict(self.view, note=f"Interner Fehler bei der Anzeige: {e}")
            time.sleep(0.4)

    def read_status(self):
        try:
            with open(self.f_status, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def poll(self):
        st = self.read_status()
        if self.proc.poll() is not None and not (st and st.get("state") in FINAL):
            st = self.read_status()  # Endstatus kann knapp nach dem Prozessende angekommen sein
            if not (st and st.get("state") in FINAL):
                out, err = (to_text(x).strip() for x in self.proc.communicate())
                msg = ("Der Löschprozess wurde unerwartet beendet." if st else
                       "Es wurden keine Administratorrechte erteilt oder der Start ist fehlgeschlagen – es wurde nichts verändert.")
                return self.finish(dict(st or {}, state="error", error=msg + (f"\n{err or out}" if err or out else "")))
        if st:
            self.view = self.render(st)
            if st.get("state") in FINAL:
                self.finish(st)

    def render(self, st):
        now, ds, n = time.time(), st.get("disks", {}), len(self.ds)
        if st.get("state") == "start":
            return self.base("launching", "Datenträger werden geprüft …", self.view["rows"])
        rows, tot, dn, etas = [], 0, 0, []
        cnt = dict(done=0, error=0, cancelled=0, wait=0, run=0, io=0, verify=0, prep=0)
        for d in self.ds:
            s = ds.get(d["id"], {})
            state, a, b = s.get("state", "wait"), s.get("done", 0), s.get("total", 0)
            tot, dn = tot + b, dn + a
            q, sp = self.dsamples[d["id"]], 0.0
            if state in ("write", "verify"):
                q.append((now, a))
                while len(q) > 2 and now - q[0][0] > 10:
                    q.popleft()
                if q[-1][0] > q[0][0]:
                    sp = (q[-1][1] - q[0][1]) / (q[-1][0] - q[0][0])
            frac, col, rate = (a / b if b else 0), "acc", ""
            if state == "done":
                su = s.get("setup")
                phase = ("Einrichten …" if su == "run" else f"Fertig · {FS_NAME[s['setup_fs']]}, {s['setup_info']}" if su == "done"
                         else "Fertig · Einrichtung fehlgeschlagen" if su == "error" else "Fertig")
                col, frac = "grn", 1
            elif state == "error":
                msg = (s.get("error") or "").splitlines()[0] if s.get("error") else ""
                msg = msg if len(msg) < 60 else msg[:58] + "…"
                phase, col = "Nicht gestartet" if s.get("at") == "wait" else msg if msg.startswith("Fehler") else "Fehler: " + msg, "red"
            elif state == "cancelled":
                phase, col = "Abgebrochen", "amb"
            elif state == "wait":
                phase = "Wartet …"
            elif state == "prepare":
                phase = "Vorbereitung …"
            elif state == "write":
                phase = f"Durchgang {s['n_pass']}/{s['n_passes']} · {PASS_NAME[s['kind']]}"
            else:
                phase, col = "Prüfung", "grn"
            cnt[state if state in ("done", "error", "cancelled", "wait") else "run"] += 1
            if state in ("write", "verify"):
                cnt["io"] += 1
                cnt["verify"] += state == "verify"
                rate = human(sp, 0) + "/s"
                if sp > 0:
                    etas.append((b - a) / sp)
                    rate += " · " + fmt_time((b - a) / sp)
                else:
                    etas.append(None)
            elif state == "prepare":
                cnt["prep"] += 1
            rows.append(self.row(d, frac, col, phase, rate, f"{frac * 100:.0f} %" if state != "wait" else ""))
        frac_all = dn / tot if tot else 0
        if n > 1:
            txt = f"{cnt['done']} von {n} fertig" + "".join(f" · {cnt[k]} {w}" for k, w in (("run", "aktiv"), ("wait", "wartend"), ("error", "mit Fehler"), ("cancelled", "abgebrochen")) if cnt[k])
        else:
            s, state = ds.get(self.ds[0]["id"], {}), ds.get(self.ds[0]["id"], {}).get("state", "wait")
            txt = {"wait": "Wird gestartet …", "prepare": "Datenträger wird vorbereitet (Partitionen entfernen / aushängen) …",
                   "verify": "Prüfung: Daten werden zurückgelesen und verglichen", "done": "Abgeschlossen", "error": "Fehler",
                   "cancelled": "Abgebrochen"}.get(state) or f"Durchgang {s['n_pass']} von {s['n_passes']}: {PASS_NAME[s['kind']]} schreiben"
        gstate = "running"
        if st.get("state") in ("ask", "setup"):
            gstate = "ask" if st["state"] == "ask" and not self.answered else "setup"
            txt = "Löschen abgeschlossen – bitte die Einrichtung als Laufwerk wählen …" if gstate == "ask" else "Laufwerke werden eingerichtet …"
        if self.cancelling and gstate == "running":
            txt = "Wird abgebrochen …"
        self.stats["el"], self.stats["done"] = fmt_time(now - self.t0), f"{human(dn)} / {human(tot)}"
        if cnt["io"]:
            if self.t_io is None:
                self.t_io = now
            self.samples.append((now, dn))
            while len(self.samples) > 2 and now - self.samples[0][0] > 10:
                self.samples.popleft()
            (t1, d1), (t2, d2) = self.samples[0], self.samples[-1]
            speed = (d2 - d1) / (t2 - t1) if t2 > t1 else 0
            avg = dn / (now - self.t_io) if now > self.t_io else 0
            if self.par:
                eta = max(etas) if etas and None not in etas and not cnt["prep"] else None
            else:
                eta = (tot - dn) / speed if speed > 0 else None
            self.tick += 1
            if self.tick % self.skip == 0:  # Verlauf über die ganze Dauer: bei Überlauf Paare mitteln
                self.hist.append(speed)
                if len(self.hist) > 300:
                    self.hist = [(x + y) / 2 for x, y in zip(self.hist[::2], self.hist[1::2])]
                    self.skip *= 2
            self.stats.update(speed=human(speed, 0) + "/s", avg=human(avg, 0) + "/s", eta=fmt_time(eta) if eta is not None else "–")
        v = self.base(gstate, txt, rows, frac_all, "grn" if cnt["io"] and cnt["verify"] == cnt["io"] else "acc")
        if gstate == "ask":
            v["ask"] = [ask_info(d) for d in self.ds if d["id"] in st.get("ask", [])]
        return v

    def finish(self, st):
        shutil.rmtree(self.tmp, ignore_errors=True)
        el, ds = time.time() - self.t0, {}
        for d in self.ds:  # nicht abgeschlossene Datenträger (Prozess beendet, Abbruch, globaler Fehler) eindeutig einordnen
            s = dict(st.get("disks", {}).get(d["id"], {}))
            if s.get("state") not in FINAL:
                s.update(at=s.get("at", s.get("state", "wait")), state="cancelled" if st["state"] == "cancelled" else "error",
                         error=s.get("error") or st.get("error") or "Der Löschvorgang wurde nicht abgeschlossen.")
            ds[d["id"]] = s
        view = self.render(dict(st, state="done" if st["state"] != "error" else "error", disks=ds))
        n, states = len(self.ds), [ds[d["id"]]["state"] for d in self.ds]
        n_ok, n_er, n_ca = states.count("done"), states.count("error"), states.count("cancelled")
        if n == 1:
            d, s = self.ds[0], ds[self.ds[0]["id"]]
            name = f"{d['id']} ({d['model'] or '?'}, {human(d['size'])})"
            res = (f"Fertig – {name} wurde {s['passes']}× überschrieben" + (" und die Prüfung bestanden (alle Bytes sind 0)." if s.get("verified") else ".")
                   if s["state"] == "done" else f"Abgebrochen – {data_note(s)}" if s["state"] == "cancelled" else f"Fehler: {s.get('error')}\n{data_note(s)}")
        elif st["state"] == "error" and n_ok == 0 and len({data_note(ds[d["id"]]) for d in self.ds}) == 1 and all(
                ds[d["id"]].get("error") == ds[self.ds[0]["id"]].get("error") for d in self.ds):
            res = f"Fehler: {ds[self.ds[0]['id']].get('error')}\n{data_note(ds[self.ds[0]['id']])}"  # ein gemeinsamer Fehler, nur einmal melden
        else:
            res = (f"{n_ok} von {n} Datenträgern erfolgreich gelöscht" + (f", {n_er} mit Fehler" if n_er else "") + (f", {n_ca} abgebrochen" if n_ca else "") + ".")
            for d in self.ds:
                s = ds[d["id"]]
                if s["state"] != "done":
                    res += f"\n• {d['id']}: {s.get('error') if s['state'] == 'error' else 'abgebrochen.'}\n   {data_note(s)}"
        wiped = [d for d in self.ds if ds[d["id"]]["state"] == "done"]
        n_se, n_su = (sum(ds[d["id"]].get("setup") == k for d in wiped) for k in ("error", "done"))
        where = {d["id"]: after_setup(ds[d["id"]]["setup_info"]) for d in wiped if ds[d["id"]].get("setup") == "done"}
        if wiped and (n_su or n_se):
            for d in wiped:
                x = ds[d["id"]]
                res += (f"\n• {d['id']}: als neues Laufwerk eingerichtet – {SCHEME_NAME[x['setup_scheme']]}, {FS_NAME[x['setup_fs']]}, "
                        f"Name „{x['setup_label']}“ ({where[d['id']]})" if x.get("setup") == "done" else
                        f"\n• {d['id']}: Einrichtung fehlgeschlagen: {x.get('setup_error')}" if x.get("setup") == "error" else
                        f"\n• {d['id']}: nicht eingerichtet")
        elif wiped:
            res += (f"\n{'Der Datenträger ist' if len(wiped) == 1 else 'Die Datenträger sind'} jetzt leer und im System nicht als Laufwerk sichtbar. "
                    f"Einrichten ist später z. B. mit {SETUP_TOOL[PLATFORM]} möglich.")
        if n_er == 0 and n_ca == 0 and n_se:
            kind, ok = "warn", "ERFOLGREICH, EINRICHTUNG FEHLGESCHLAGEN"
        elif n_er == 0 and n_ca == 0:
            kind, ok = "ok", "ERFOLGREICH"
        elif n_er:
            kind, ok = "err", "FEHLGESCHLAGEN" if n_ok == 0 else "TEILWEISE ERFOLGREICH"
        else:
            kind, ok = "warn", "ABGEBROCHEN" if n_ok == 0 else "TEILWEISE ERFOLGREICH"
        self.stats.update(speed="–", eta="0:00:00" if n_ok == n else "–")
        phase = ("Abgeschlossen" if n_ok == n else "Beendet mit Fehlern" if n_er else "Abgebrochen") if n == 1 or n_ok == n else f"Beendet: {n_ok} von {n} erfolgreich"
        frac = 1 if n_ok == n else view["frac"]
        head = [f"Schredder {VERSION} – Löschprotokoll", "=" * 44, f"Gesamtergebnis: {ok}",
                f"Beginn:         {datetime.fromtimestamp(self.t0):%Y-%m-%d %H:%M:%S}", f"Ende:           {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"Dauer:          {fmt_time(el)}", f"Computer:       {platform.node()} ({platform.platform()})",
                f"Methode:        {self.mode['name']} – {' → '.join(PASS_NAME[p] for p in self.mode['passes'])}",
                f"Ablauf:         {'gleichzeitig' if self.par else 'nacheinander'}, Blockgröße {CHUNK >> 20} MiB"]
        for d in self.ds:
            s = ds[d["id"]]
            r = {"done": "ERFOLGREICH", "error": "FEHLGESCHLAGEN", "cancelled": "ABGEBROCHEN"}[s["state"]]
            head += ["", f"--- {d['id']} ---", f"Datenträger:    {d['id']} – {d['model'] or '?'}", f"Seriennummer:   {d['serial'] or '–'}",
                     f"Größe:          {d['size']} Bytes ({human(d['size'])})", f"Anschluss/Typ:  {d['bus'] or '?'} / {d['kind']}",
                     f"Inhalt vorher:  {', '.join(d['volumes']) or '–'}", f"Ergebnis:       {r}"]
            if s["state"] == "done":
                head += [f"Prüfung:        {'bestanden (alle Bytes sind 0)' if s.get('verified') else 'nicht durchgeführt'}",
                         "Einrichtung:    " + (f"{SCHEME_NAME[s['setup_scheme']]} + {FS_NAME[s['setup_fs']]}, Name „{s['setup_label']}“, {where[d['id']]}"
                                               if s.get("setup") == "done" else f"FEHLGESCHLAGEN: {s.get('setup_error')}"
                                               if s.get("setup") == "error" else "nicht eingerichtet (Datenträger leer, nicht als Laufwerk sichtbar)")]
            else:
                head += [f"Meldung:        {s.get('error') or 'Abgebrochen'}", f"Daten:          {data_note(s)}"]
            head += [f"Dauer:          {fmt_time(s.get('seconds', 0))}", f"Parallelität:   {s.get('threads', '–')} Threads"]
            if d["kind"] in ("SSD", "NVMe", "Flash"):
                head += ["Hinweis:        Flash-Speicher: Wear-Leveling kann Restdaten behalten."]
            if s.get("detail"):
                head += [f"Fehlerdetails:\n{s['detail']}"]
        if st.get("detail"):
            head += ["", f"Fehlerdetails:\n{st['detail']}"]
        self.report = "\n".join(head)
        v = self.base("finished", phase, view["rows"], frac, {"ok": "grn", "warn": "amb", "err": "red"}[kind])
        v["pct"] = "100 %" if n_ok == n else v["pct"]
        v["result"] = dict(kind=kind, text=res)
        self.view = v
        self.done = True  # erst jetzt: Ansicht und Protokoll stehen bereit

    # ── Eingaben der Oberfläche ──
    def cancel(self):
        self.cancelling = True
        open(self.f_cancel, "w").close()

    def answer_setup(self, items):
        self.answered = True
        for _ in range(40):  # unter Windows scheitert das Ersetzen kurz, solange der Worker die Datei liest
            try:
                with open(self.f_setup + ".tmp", "w", encoding="utf-8") as f:
                    json.dump({"setup": items}, f)
                os.replace(self.f_setup + ".tmp", self.f_setup)
                return
            except OSError:
                time.sleep(0.05)
        raise Bad("Die Antwort konnte nicht an den Löschprozess übergeben werden (er läuft vermutlich nicht mehr).", 500)


class Bad(Exception):
    """Ungültige Anfrage der Oberfläche (HTTP 400/409)."""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class State:
    """Zustand der Anwendung (eine Instanz je Prozess): letzte Datenträgerliste und der aktuelle Auftrag."""

    def __init__(self):
        self.token, self.disks, self.run = secrets.token_urlsafe(24), {}, None
        self.last_ping, self.closing, self.lock = time.time(), None, threading.Lock()

    @property
    def busy(self):
        return bool(self.run and not self.run.done)

    def info(self):
        return dict(version=VERSION, python=platform.python_version(), system=platform.platform(), platform=PLATFORM, admin=is_admin(),
                    modes=[dict(name=m["name"], sub=m["sub"], info=m["info"], passes=[PASS_NAME[p] for p in m["passes"]]) for m in MODES],
                    perf=[dict(name=n, value=v) for n, v in PERF], flow=[dict(name=n, value=v) for n, v in FLOW],
                    tool=SETUP_TOOL[PLATFORM], run=self.run.view if self.run else None, title=TITLE)

    def scan(self):
        if self.busy:
            raise Bad("Während eines Löschvorgangs ist keine neue Suche möglich.", 409)
        with self.lock:
            disks = list_disks()
            self.disks = {d["id"]: d for d in disks}
        return [disk_info(d) for d in disks]

    def start(self, p):
        if self.busy:
            raise Bad("Es läuft bereits ein Löschvorgang.", 409)
        ids = p.get("ids")
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or any(i not in self.disks for i in ids):
            raise Bad("Ungültige Datenträgerauswahl. Bitte die Liste aktualisieren.")
        ds = [self.disks[i] for i in ids]
        if any(d["locked"] for d in ds):
            raise Bad("Gesperrte Datenträger können nicht gelöscht werden.")
        mode, threads = p.get("mode"), p.get("threads", 0)
        if not isinstance(mode, int) or not 0 <= mode < len(MODES) or threads not in [v for _, v in PERF]:
            raise Bad("Ungültige Methode.")
        par = bool(p.get("parallel", True)) if len(ds) > 1 else True
        word = ds[0]["id"] if len(ds) == 1 else str(len(ds))
        if str(p.get("confirm", "")).strip().lower() != word.lower():
            raise Bad("Die Bestätigung stimmt nicht überein.")
        plan = p.get("setup")
        if plan is not None:
            if not isinstance(plan, list):
                raise Bad("Ungültige Angaben für die Einrichtung.")
            for it in plan:
                d = self.disks.get(it.get("id")) if isinstance(it, dict) else None
                if d is None or d not in ds or it.get("scheme") not in SCHEME_NAME or it.get("fs") not in setup_fs(d) or (
                        it["scheme"] == "mbr" and (d["size"] > MBR_MAX or it["fs"] == "apfs")):
                    raise Bad("Ungültige Angaben für die Einrichtung.")
            plan = [dict(id=it["id"], scheme=it["scheme"], fs=it["fs"], label=str(it.get("label") or "")[:64]) for it in plan]
        try:
            self.run = Run(ds, MODES[mode], bool(p.get("verify", True)), threads, par, plan)
        except Exception as e:
            raise Bad(f"Löschprozess konnte nicht gestartet werden: {e}", 500)


def open_ui(url):
    """Bevorzugt ein eigenes App-Fenster (Edge/Chrome ohne Adressleiste), sonst der Standardbrowser."""
    flags = [f"--app={url}", "--window-size=1100,900"]
    try:
        if PLATFORM == "win":
            roots = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
            for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"):
                exe = next((os.path.join(r, sub) for r in roots if r and os.path.isfile(os.path.join(r, sub))), None)
                if exe:
                    return subprocess.Popen([exe] + flags)
        elif PLATFORM == "macos":
            for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
                if os.path.isdir(f"/Applications/{app}.app"):
                    return subprocess.Popen(["open", "-na", app, "--args"] + flags)
        else:
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
                exe = shutil.which(name)
                if exe:
                    return subprocess.Popen([exe] + flags)
    except OSError:
        pass
    webbrowser.open(url)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version, sys_version = "Schredder", ""

    def log_message(self, *a):
        pass

    @property
    def app(self):
        return self.server.app

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=()):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(data))), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                     ("Content-Security-Policy", CSP), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer")) + tuple(extra):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        """Nur die eigene Seite darf zugreifen: Host (gegen DNS-Rebinding), Herkunft und Token prüfen."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in hosts}:
            return False
        if self.path.startswith("/api/"):
            return hmac.compare_digest(self.headers.get("X-Schredder-Token", ""), self.app.token)
        return True

    def handle_any(self, post):
        if not self.allowed():
            return self.send(403, dict(error="Zugriff verweigert."))
        path = self.path.split("?")[0]
        try:
            if not post and path in ("/", "/app.css", "/app.js"):
                body, ctype = {"/": (UI_HTML, "text/html; charset=utf-8"), "/app.css": (UI_CSS, "text/css; charset=utf-8"),
                               "/app.js": (UI_JS, "text/javascript; charset=utf-8")}[path]
                return self.send(200, body, ctype)
            if not path.startswith("/api/"):
                return self.send(404, dict(error="Nicht gefunden."))
            app, p = self.app, {}
            if post:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= 1 << 20:
                    raise Bad("Anfrage zu groß.")
                p = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(p, dict):
                    raise Bad("Ungültige Anfrage.")
            if path != "/api/bye":
                app.last_ping, app.closing = time.time(), None
            if not post and path == "/api/state":
                return self.send(200, app.info())
            if not post and path == "/api/run":
                return self.send(200, app.run.view if app.run else dict(state="idle"))
            if not post and path == "/api/report":
                if not (app.run and app.run.report):
                    raise Bad("Noch kein Protokoll vorhanden.", 404)
                return self.send(200, app.run.report, "text/plain; charset=utf-8")
            if post and path == "/api/scan":
                return self.send(200, dict(disks=app.scan()))
            if post and path == "/api/start":
                app.start(p)
                return self.send(200, dict(ok=True))
            if post and path == "/api/cancel":
                if app.busy:
                    app.run.cancel()
                return self.send(200, dict(ok=True))
            if post and path == "/api/setup":
                if not (app.busy and isinstance(p.get("items"), list)):
                    raise Bad("Der Löschvorgang ist bereits beendet, die Einrichtung ist nicht mehr möglich. "
                              f"Der Datenträger kann mit {SETUP_TOOL[PLATFORM]} eingerichtet werden.", 409)
                app.run.answer_setup(p["items"])
                return self.send(200, dict(ok=True))
            if post and path == "/api/back":
                if app.busy:
                    raise Bad("Es läuft noch ein Löschvorgang.", 409)
                app.run = None
                return self.send(200, dict(ok=True))
            if post and path == "/api/ping":
                return self.send(200, dict(ok=True))
            if post and path == "/api/bye":
                app.closing = time.time() + 4  # Seite wurde geschlossen (oder nur neu geladen: dann kommt gleich ein Ping)
                return self.send(200, dict(ok=True))
            return self.send(404, dict(error="Nicht gefunden."))
        except Bad as e:
            self.send(e.code, dict(error=str(e)))
        except (ValueError, KeyError, TypeError) as e:
            self.send(400, dict(error=f"Ungültige Anfrage: {e}"))
        except Exception as e:
            self.send(500, dict(error=f"{type(e).__name__}: {e}"))

    def do_GET(self):
        self.handle_any(False)

    def do_POST(self):
        self.handle_any(True)


def make_server(port=0):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.app = State()
    return httpd


def serve(open_browser=True):
    httpd = make_server()
    app, port = httpd.app, httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/#t={app.token}"

    def watch():  # beendet das Programm, wenn die Seite geschlossen wurde – aber nie mitten in einem Löschvorgang
        while True:
            time.sleep(0.5)
            if app.busy:
                continue
            now = time.time()
            if (app.closing and now > app.closing) or now - app.last_ping > IDLE_EXIT:
                return httpd.shutdown()
    threading.Thread(target=watch, daemon=True).start()
    say(f"Schredder {VERSION} läuft: {url}")
    if open_browser:
        open_ui(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def crash(e):
    """Fehler beim Start sichtbar machen (unter Windows läuft das Programm ohne Konsole)."""
    log = os.path.join(tempfile.gettempdir(), "schredder.log")
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
    except OSError:
        pass
    msg = f"Schredder konnte nicht starten:\n{type(e).__name__}: {e}\n\nDetails: {log}"
    say(msg)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, "Schredder", 0x10)
        except Exception:
            pass
    return 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        sys.exit(worker(sys.argv[2]))
    try:
        serve(open_browser="--no-browser" not in sys.argv)
    except Exception as e:
        sys.exit(crash(e))

# ───────────────────────── Weboberfläche (aus src/ui.* eingebettet) ─────────────────────────
UI_HTML = (
    '<!doctype html>\n'
    '<html lang="de">\n'
    '<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="color-scheme" content="light dark">\n'
    '<title>Schredder – Datenträger sicher löschen</title>\n'
    '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'%3E%3Crect width=\'32\' height=\'32\' rx=\'7\' fill=\'%230f172a\'/%3E%3Crect x=\'5\' y=\'6\' width=\'22\' height=\'8\' fill=\'%23f8fafc\'/%3E%3Cpath d=\'M7 17h2v9H7zM12 17h2v9h-2zM17 17h2v9h-2zM22 17h2v9h-2z\' fill=\'%2360a5fa\'/%3E%3C/svg%3E">\n'
    '<link rel="stylesheet" href="/app.css">\n'
    '</head>\n'
    '<body>\n'
    '<header class="top">\n'
    '  <div class="brand">\n'
    '    <svg class="logo" viewBox="0 0 46 46" aria-hidden="true"><rect x="5" y="7" width="36" height="14" fill="#f8fafc"/><g fill="#60a5fa"><rect x="8" y="25" width="3" height="16"/><rect x="14" y="25" width="3" height="16"/><rect x="20" y="25" width="3" height="16"/><rect x="26" y="25" width="3" height="16"/><rect x="32" y="25" width="3" height="16"/><rect x="38" y="25" width="3" height="16"/></g></svg>\n'
    '    <div><h1>Schredder</h1><p>Datenträger sicher und schnell löschen</p></div>\n'
    '  </div>\n'
    '  <div class="meta"><span id="ver"></span><span id="rights"></span></div>\n'
    '</header>\n'
    '\n'
    '<main>\n'
    '  <p class="warn">Achtung: Alle Daten auf den gewählten Datenträgern werden unwiderruflich überschrieben und lassen sich nicht wiederherstellen.</p>\n'
    '  <p class="warn" id="offline" hidden>Die Verbindung zu Schredder wurde unterbrochen. Bitte das Programm neu starten.</p>\n'
    '\n'
    '  <section id="v-select" class="stack">\n'
    '    <article class="card">\n'
    '      <div class="card-h">\n'
    '        <h2><span class="n">1</span> Datenträger wählen</h2>\n'
    '        <div class="tools"><span id="scan-info" class="mute"></span><button class="btn" id="b-all" type="button">Alle auswählen</button><button class="btn" id="b-refresh" type="button">Aktualisieren</button></div>\n'
    '      </div>\n'
    '      <div class="tablewrap">\n'
    '        <table class="disks" id="disks">\n'
    '          <thead><tr><th><span class="sr" hidden>Auswahl</span></th><th>Name</th><th>Modell</th><th class="r">Größe</th><th>Typ</th><th>Inhalt / Status</th></tr></thead>\n'
    '          <tbody></tbody>\n'
    '        </table>\n'
    '      </div>\n'
    '      <p class="detail mute" id="detail"></p>\n'
    '    </article>\n'
    '\n'
    '    <article class="card">\n'
    '      <div class="card-h"><h2><span class="n">2</span> Löschmethode</h2></div>\n'
    '      <div class="modes" id="modes" role="radiogroup" aria-label="Löschmethode"></div>\n'
    '      <div class="opts">\n'
    '        <label class="chk"><input type="checkbox" id="o-verify" checked> Danach prüfen (alles zurücklesen)</label>\n'
    '        <label class="mute">Nach dem Löschen: <select id="o-setup"><option value="auto">Einrichten – empfohlenes Format</option><option value="choose">Einrichten – Format selbst wählen</option><option value="none">Nicht einrichten</option></select></label>\n'
    '        <label class="mute">Mehrere Datenträger: <select id="o-flow"></select></label>\n'
    '        <label class="mute">Leistung: <select id="o-perf"></select></label>\n'
    '      </div>\n'
    '    </article>\n'
    '  </section>\n'
    '\n'
    '  <section id="v-run" hidden>\n'
    '    <article class="card">\n'
    '      <div class="card-h"><h2 id="r-title"></h2><div class="tools"><span class="pct" id="r-pct"></span><button class="btn" id="b-cancel" type="button">Abbrechen</button></div></div>\n'
    '      <p class="phase" id="r-phase"></p>\n'
    '      <div class="meter" id="r-meter"><i></i></div>\n'
    '      <dl class="stats">\n'
    '        <div><dt>Geschwindigkeit</dt><dd id="s-speed">–</dd></div>\n'
    '        <div><dt>Durchschnitt</dt><dd id="s-avg">–</dd></div>\n'
    '        <div><dt>Vergangen</dt><dd id="s-el">–</dd></div>\n'
    '        <div><dt>Verbleibend</dt><dd id="s-eta">–</dd></div>\n'
    '        <div><dt>Verarbeitet</dt><dd id="s-done">–</dd></div>\n'
    '      </dl>\n'
    '      <div class="rows" id="r-rows"></div>\n'
    '      <canvas id="r-chart" aria-label="Geschwindigkeitsverlauf"></canvas>\n'
    '      <div id="r-result" hidden>\n'
    '        <div class="banner" id="r-banner"></div>\n'
    '        <div class="btns"><button class="btn" id="b-report" type="button">Protokoll speichern …</button><button class="btn" id="b-back" type="button">Zurück</button></div>\n'
    '      </div>\n'
    '      <p class="note" id="r-note" hidden></p>\n'
    '    </article>\n'
    '  </section>\n'
    '</main>\n'
    '\n'
    '<footer class="bar" id="bar">\n'
    '  <span id="hint"></span>\n'
    '  <button class="btn danger" id="b-go" type="button" disabled>Datenträger löschen …</button>\n'
    '</footer>\n'
    '<p class="about" id="about"></p>\n'
    '\n'
    '<dialog id="d-confirm"></dialog>\n'
    '<dialog id="d-setup"></dialog>\n'
    '<dialog id="d-msg"></dialog>\n'
    '<script src="/app.js"></script>\n'
    '</body>\n'
    '</html>\n'
)
UI_CSS = (
    ':root {\n'
    '  --bg: #f1f4f9; --card: #ffffff; --ink: #1d2433; --mute: #667085; --line: #d9dee8; --navy: #0f172a; --trough: #e4e8f0;\n'
    '  --acc: #2563eb; --acc-d: #1d4ed8; --acc-l: #eaf1ff; --red: #dc2626; --red-d: #b91c1c; --red-l: #fef2f2; --red-ln: #fecaca;\n'
    '  --grn: #16a34a; --grn-l: #f0fdf4; --grn-ln: #bbf7d0; --amb: #b45309; --amb-l: #fffbeb; --amb-ln: #fde68a;\n'
    '  --hover: #f3f5f9; --head: #f6f8fb; --btn-bd: #c5ccd9; --chart: #dbe7ff;\n'
    '}\n'
    '@media (prefers-color-scheme: dark) {\n'
    '  :root {\n'
    '    --bg: #0b1020; --card: #141b2e; --ink: #e6eaf2; --mute: #9aa4b8; --line: #2a344d; --navy: #070b17; --trough: #232d45;\n'
    '    --acc: #60a5fa; --acc-d: #3b82f6; --acc-l: #172554; --red: #f87171; --red-d: #fca5a5; --red-l: #2a1215; --red-ln: #5b1d22;\n'
    '    --grn: #4ade80; --grn-l: #10231a; --grn-ln: #1f5136; --amb: #fbbf24; --amb-l: #2a2008; --amb-ln: #5a4410;\n'
    '    --hover: #1b2440; --head: #182138; --btn-bd: #3a4666; --chart: #1e3a8a;\n'
    '  }\n'
    '}\n'
    '* { box-sizing: border-box; }\n'
    'html { font-size: 15px; }\n'
    'body {\n'
    '  margin: 0; min-height: 100vh; display: flex; flex-direction: column; background: var(--bg); color: var(--ink);\n'
    '  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", system-ui, Roboto, "Helvetica Neue", Ubuntu, sans-serif;\n'
    '  line-height: 1.4;\n'
    '}\n'
    '[hidden] { display: none !important; }\n'
    'h1, h2, h3, p { margin: 0; }\n'
    '.mute { color: var(--mute); }\n'
    '.r { text-align: right; }\n'
    '\n'
    '.top { background: var(--navy); color: #fff; display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: center; justify-content: space-between; padding: 12px 24px; }\n'
    '.brand { display: flex; align-items: center; gap: 14px; }\n'
    '.logo { width: 42px; height: 42px; }\n'
    '.top h1 { font-size: 1.55rem; font-weight: 700; line-height: 1.1; }\n'
    '.top p, .meta { color: #94a3b8; font-size: .85rem; }\n'
    '.meta { display: flex; flex-direction: column; align-items: flex-end; }\n'
    '\n'
    'main { flex: 1; width: 100%; max-width: 1020px; margin: 0 auto; padding: 16px 24px 24px; display: flex; flex-direction: column; gap: 14px; }\n'
    '.warn { background: var(--red-l); border: 1px solid var(--red-ln); color: var(--red-d); padding: 9px 14px; border-radius: 6px; }\n'
    '.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px 18px 16px; }\n'
    '.card-h { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; justify-content: space-between; margin-bottom: 10px; }\n'
    '.card h2, .card-h h2 { font-size: 1.1rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }\n'
    '.n { display: inline-grid; place-items: center; width: 1.6em; height: 1.6em; border-radius: 50%; background: var(--acc-l); color: var(--acc); font-size: .85em; }\n'
    '.tools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }\n'
    '\n'
    '.btn {\n'
    '  font: inherit; color: var(--ink); background: var(--card); border: 1px solid var(--btn-bd); border-radius: 6px; padding: 7px 14px; cursor: pointer;\n'
    '}\n'
    '.btn:hover:not(:disabled) { background: var(--hover); }\n'
    '.btn:disabled { opacity: .5; cursor: default; }\n'
    '.btn.danger { background: var(--red); border-color: var(--red-d); color: #fff; font-weight: 700; padding: 9px 20px; }\n'
    '.btn.danger:hover:not(:disabled) { background: var(--red-d); }\n'
    '.btn.primary { background: var(--acc); border-color: var(--acc-d); color: #fff; font-weight: 700; padding: 9px 20px; }\n'
    '.btn.primary:hover:not(:disabled) { background: var(--acc-d); }\n'
    ':is(.btn, input, select, summary):focus-visible { outline: 2px solid var(--acc); outline-offset: 2px; }\n'
    '\n'
    '.tablewrap { border: 1px solid var(--line); border-radius: 6px; overflow-x: auto; }\n'
    'table.disks { width: 100%; border-collapse: collapse; }\n'
    '.disks th { background: var(--head); color: var(--mute); font-size: .85rem; text-align: left; padding: 7px 10px; white-space: nowrap; position: sticky; top: 0; }\n'
    '.disks td { padding: 8px 10px; border-top: 1px solid var(--line); vertical-align: middle; }\n'
    '.disks th:first-child, .disks td:first-child { width: 36px; text-align: center; padding-right: 0; }\n'
    '.disks tbody tr { cursor: pointer; }\n'
    '.disks tbody tr:hover { background: var(--hover); }\n'
    '.disks tbody tr.sel { background: var(--acc-l); }\n'
    '.disks tbody tr.locked { color: var(--mute); cursor: default; opacity: .75; }\n'
    '.disks tbody tr.empty td { text-align: center; color: var(--mute); cursor: default; padding: 18px; }\n'
    '.disks input[type=checkbox] { width: 17px; height: 17px; accent-color: var(--acc); }\n'
    '.detail { margin-top: 8px; white-space: pre-line; font-size: .88rem; min-height: 3.9em; }\n'
    '\n'
    '.modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }\n'
    '.mode { text-align: left; font: inherit; color: inherit; background: var(--card); border: 2px solid var(--line); border-radius: 6px; padding: 10px 12px; cursor: pointer; }\n'
    '.mode b { display: block; }\n'
    '.mode .sub { display: block; color: var(--acc); font-weight: 700; font-size: .85rem; }\n'
    '.mode .info { display: block; color: var(--mute); font-size: .85rem; margin-top: 4px; }\n'
    '.mode.on { border-color: var(--acc); background: var(--acc-l); }\n'
    '.opts { display: flex; flex-wrap: wrap; gap: 10px 22px; align-items: center; margin-top: 12px; }\n'
    '.opts label { display: flex; align-items: center; gap: 8px; }\n'
    '.opts .chk { margin-right: auto; }\n'
    'select, input[type=text] {\n'
    '  font: inherit; color: var(--ink); background: var(--card); border: 1px solid var(--btn-bd); border-radius: 6px; padding: 6px 8px;\n'
    '}\n'
    'select:disabled { opacity: .5; }\n'
    '\n'
    '.bar { position: sticky; bottom: 0; background: var(--bg); border-top: 1px solid var(--line); padding: 10px 24px; display: flex; gap: 14px; align-items: center; justify-content: space-between; }\n'
    '.bar #hint { color: var(--mute); }\n'
    '.about { text-align: center; color: var(--mute); font-size: .78rem; padding: 6px 12px 12px; }\n'
    '\n'
    '.pct { font-size: 1.5rem; font-weight: 700; color: var(--acc); }\n'
    '.pct[data-c=grn] { color: var(--grn); } .pct[data-c=red] { color: var(--red-d); } .pct[data-c=amb] { color: var(--amb); }\n'
    '.phase { font-weight: 700; margin-bottom: 8px; }\n'
    '.meter { height: 14px; background: var(--trough); border-radius: 3px; overflow: hidden; }\n'
    '.meter i { display: block; height: 100%; width: calc(var(--v, 0) * 100%); min-width: 2px; background: var(--acc); transition: width .3s; }\n'
    '[data-c=grn] > i, .bar-s[data-c=grn] i { background: var(--grn); } [data-c=red] > i, .bar-s[data-c=red] i { background: var(--red); } [data-c=amb] > i, .bar-s[data-c=amb] i { background: var(--amb); }\n'
    '.stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px 12px; margin: 12px 0; }\n'
    '.stats div { min-width: 0; } .stats dt { color: var(--mute); font-size: .82rem; } .stats dd { margin: 0; font-weight: 700; }\n'
    '.rows { border: 1px solid var(--line); border-radius: 6px; margin-bottom: 10px; max-height: 300px; overflow-y: auto; }\n'
    '.rowd { padding: 8px 12px; border-top: 1px solid var(--line); display: grid; grid-template-columns: 1fr auto; gap: 2px 12px; }\n'
    '.rowd:first-child { border-top: 0; }\n'
    '.rowd .nm { font-weight: 700; } .rowd .nm span { font-weight: 400; color: var(--mute); margin-left: 8px; }\n'
    '.rowd .pc { font-weight: 700; text-align: right; color: var(--acc); }\n'
    '.rowd[data-c=grn] .pc { color: var(--grn); } .rowd[data-c=red] .pc { color: var(--red-d); } .rowd[data-c=amb] .pc { color: var(--amb); }\n'
    '.bar-s { height: 8px; background: var(--trough); border-radius: 2px; overflow: hidden; align-self: center; }\n'
    '.bar-s i { display: block; height: 100%; width: calc(var(--v, 0) * 100%); min-width: 2px; background: var(--acc); transition: width .3s; }\n'
    '.rowd .bar-s { grid-column: 1 / -1; }\n'
    '.rowd .ph, .rowd .rt { font-size: .85rem; }\n'
    '.rowd .rt { text-align: right; color: var(--mute); }\n'
    '#r-chart { width: 100%; height: 130px; display: block; border: 1px solid var(--line); border-radius: 6px; background: var(--hover); }\n'
    '.banner { margin-top: 12px; padding: 10px 14px; border-radius: 6px; border: 1px solid; white-space: pre-line; }\n'
    '.banner.ok { background: var(--grn-l); border-color: var(--grn-ln); color: var(--grn); }\n'
    '.banner.warn { background: var(--amb-l); border-color: var(--amb-ln); color: var(--amb); }\n'
    '.banner.err { background: var(--red-l); border-color: var(--red-ln); color: var(--red-d); }\n'
    '.btns { display: flex; gap: 10px; justify-content: flex-end; margin-top: 12px; flex-wrap: wrap; }\n'
    '.note { margin-top: 10px; color: var(--amb); }\n'
    '\n'
    'dialog { border: 0; border-radius: 10px; padding: 0; background: var(--card); color: var(--ink); width: min(760px, calc(100vw - 24px)); max-height: calc(100vh - 24px); box-shadow: 0 10px 40px rgb(0 0 0 / .35); }\n'
    'dialog::backdrop { background: rgb(15 23 42 / .55); }\n'
    'dialog form, .dlg { padding: 20px 24px; display: flex; flex-direction: column; gap: 12px; max-height: calc(100vh - 24px); overflow-y: auto; }\n'
    'dialog h3 { font-size: 1.3rem; } dialog h3.red { color: var(--red); }\n'
    '#d-msg { width: min(460px, calc(100vw - 24px)); } #d-msg p { white-space: pre-line; }\n'
    '.box { background: var(--red-l); border: 1px solid var(--red-ln); border-radius: 6px; padding: 8px 12px; max-height: 260px; overflow-y: auto; }\n'
    '.box b { display: block; } .box small { display: block; color: var(--mute); margin-bottom: 6px; }\n'
    '#d-confirm input[type=text] { width: 100%; border-width: 2px; padding: 8px 10px; }\n'
    '.sdisk { border: 1px solid var(--line); border-radius: 6px; padding: 10px 14px; display: flex; flex-direction: column; gap: 8px; }\n'
    '.sdisk .ctl { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; }\n'
    '.sdisk .ctl label { display: flex; align-items: center; gap: 6px; color: var(--mute); font-size: .88rem; }\n'
    '.sdisk .why { color: var(--mute); font-size: .85rem; }\n'
    '.sdisk > label { display: flex; align-items: center; gap: 8px; font-weight: 700; }\n'
    '\n'
    '@media (max-width: 820px) {\n'
    '  .modes { grid-template-columns: 1fr; }\n'
    '  .stats { grid-template-columns: repeat(2, 1fr); }\n'
    '  main { padding: 12px; } .top, .bar { padding-left: 12px; padding-right: 12px; }\n'
    '}\n'
)
UI_JS = (
    '"use strict";\n'
    'const $ = (id) => document.getElementById(id);\n'
    'const TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || "";\n'
    'const TITLE = "Schredder – Datenträger sicher löschen";\n'
    'const S = { info: null, disks: [], sel: new Set(), peek: null, mode: 0, view: null, scanning: false, looping: false, err: "", offline: false };\n'
    'const sleep = (ms) => new Promise((r) => setTimeout(r, ms));\n'
    '\n'
    '// Element bauen – Texte immer als Textknoten (Namen von Datenträgern dürfen nie als HTML gedeutet werden)\n'
    'function h(tag, attrs, ...kids) {\n'
    '  const e = document.createElement(tag);\n'
    '  for (const [k, v] of Object.entries(attrs || {})) {\n'
    '    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);\n'
    '    else if (v === true) e.setAttribute(k, "");\n'
    '    else if (v !== false && v != null) e.setAttribute(k, v);\n'
    '  }\n'
    '  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));\n'
    '  return e;\n'
    '}\n'
    '\n'
    '// Hinweis (nur ein Knopf) oder Rückfrage (zwei Knöpfe) als Dialog der Seite – alert/confirm sperren manche Browser\n'
    'function msg(title, text, yes, no) {\n'
    '  return new Promise((resolve) => {\n'
    '    const d = $("d-msg");\n'
    '    d.returnValue = "";\n'
    '    d.addEventListener("close", () => resolve(d.returnValue === "yes"), { once: true });\n'
    '    d.replaceChildren(h("div", { class: "dlg" }, h("h3", {}, title), h("p", {}, text),\n'
    '      h("div", { class: "btns" }, no && h("button", { class: "btn", type: "button", onclick: () => d.close("no") }, no),\n'
    '        h("button", { class: "btn primary", type: "button", onclick: () => d.close("yes") }, yes))));\n'
    '    d.showModal();\n'
    '  });\n'
    '}\n'
    '\n'
    'async function api(path, body) {\n'
    '  const r = await fetch("/api/" + path, {\n'
    '    method: body === undefined ? "GET" : "POST",\n'
    '    headers: { "X-Schredder-Token": TOKEN, "Content-Type": "application/json" },\n'
    '    body: body === undefined ? undefined : JSON.stringify(body),\n'
    '  });\n'
    '  let data = {};\n'
    '  try { data = await r.json(); } catch (e) { /* leere Antwort */ }\n'
    '  if (!r.ok) throw new Error(data.error || "HTTP " + r.status);\n'
    '  return data;\n'
    '}\n'
    '\n'
    'function human(n, dec = 1) {\n'
    '  const u = ["B", "KB", "MB", "GB", "TB"];\n'
    '  let i = 0;\n'
    '  while (n >= 1000 && i < 4) { n /= 1000; i++; }\n'
    '  return (i ? n.toFixed(dec) : String(Math.round(n))).replace(".", ",") + " " + u[i];\n'
    '}\n'
    'const typ = (d) => (!d.bus || d.bus.toLowerCase() === d.kind.toLowerCase() ? d.kind : `${d.bus} · ${d.kind}`);\n'
    'const selected = () => S.disks.filter((d) => S.sel.has(d.id));\n'
    'const active = () => S.view && ["launching", "running", "ask", "setup"].includes(S.view.state);\n'
    '\n'
    '// ───── Datenträgerliste ─────\n'
    'function renderDisks() {\n'
    '  const tb = $("disks").tBodies[0];\n'
    '  tb.replaceChildren();\n'
    '  if (!S.disks.length) {\n'
    '    tb.append(h("tr", { class: "empty" }, h("td", { colspan: 6 }, S.scanning ? "Suche Datenträger …" : "Keine Datenträger gefunden. Datenträger anschließen und „Aktualisieren“ klicken.")));\n'
    '  }\n'
    '  for (const d of S.disks) {\n'
    '    const cb = h("input", { type: "checkbox", disabled: !!d.locked, "aria-label": d.id });\n'
    '    cb.checked = S.sel.has(d.id);\n'
    '    const tr = h("tr", { class: (d.locked ? "locked " : "") + (cb.checked ? "sel" : ""), title: d.locked ? "Gesperrt: " + d.locked : "" },\n'
    '      h("td", {}, cb), h("td", {}, d.id), h("td", {}, d.model || "–"), h("td", { class: "r" }, d.size_h), h("td", {}, typ(d)),\n'
    '      h("td", {}, d.locked ? "Gesperrt: " + d.locked : d.volumes.join(", ") || "–"));\n'
    '    tr.addEventListener("click", (e) => {\n'
    '      if (d.locked) { S.peek = d; return updateSel(); }\n'
    '      if (e.target !== cb) cb.checked = !cb.checked;\n'
    '      if (cb.checked) S.sel.add(d.id); else S.sel.delete(d.id);\n'
    '      S.peek = null;\n'
    '      tr.classList.toggle("sel", cb.checked);\n'
    '      updateSel();\n'
    '    });\n'
    '    tb.append(tr);\n'
    '  }\n'
    '  const n = S.disks.filter((d) => !d.locked).length;\n'
    '  $("scan-info").textContent = S.scanning ? "Suche Datenträger …" : `${n} löschbar` + (S.disks.length > n ? `, ${S.disks.length - n} gesperrt` : "");\n'
    '}\n'
    '\n'
    'function updateSel() {\n'
    '  const sel = selected();\n'
    '  let txt = S.err;\n'
    '  if (!txt && sel.length === 1) {\n'
    '    const d = sel[0];\n'
    '    txt = `${d.model || "Unbekanntes Modell"}${d.serial ? "  ·  Seriennr. " + d.serial : ""}\\n${d.size_h} (${d.size.toLocaleString("de-DE")} Bytes)  ·  ${d.bus || "?"}  ·  ${d.kind}  ·  Gerät ${d.path}`;\n'
    '    if (["SSD", "NVMe", "Flash"].includes(d.kind)) txt += "\\nHinweis: Flash-Speicher kann Restdaten behalten – für höchste Sicherheit zusätzlich Secure Erase des Herstellers nutzen.";\n'
    '  } else if (!txt && sel.length > 1) {\n'
    '    const names = sel.map((d) => d.id).join(", ");\n'
    '    txt = `${sel.length} Datenträger ausgewählt – zusammen ${human(sel.reduce((a, d) => a + d.size, 0))}\\n${names.length < 100 ? names : names.slice(0, 97) + "…"}`;\n'
    '  } else if (!txt && S.peek) {\n'
    '    txt = `${S.peek.model || "Unbekanntes Modell"}  ·  ${S.peek.size_h}\\nGesperrt: ${S.peek.locked}`;\n'
    '  }\n'
    '  $("detail").textContent = txt;\n'
    '  const d = $("detail");\n'
    '  d.style.color = S.err || (!sel.length && S.peek) ? "var(--red)" : "";\n'
    '  $("b-go").disabled = !sel.length || active() || S.offline;\n'
    '  $("b-go").textContent = sel.length > 1 ? `${sel.length} Datenträger löschen …` : "Datenträger löschen …";\n'
    '  $("o-flow").disabled = sel.length < 2;\n'
    '  $("hint").textContent = sel.length ? (S.info && S.info.admin ? "" : "Beim Löschen werden Administratorrechte angefordert.") : "Einen oder mehrere Datenträger anhaken.";\n'
    '}\n'
    '\n'
    'async function scan() {\n'
    '  S.scanning = true; S.err = ""; S.sel.clear(); S.peek = null;\n'
    '  $("b-refresh").disabled = true;\n'
    '  renderDisks(); updateSel();\n'
    '  try { S.disks = (await api("scan", {})).disks; }\n'
    '  catch (e) { S.disks = []; S.err = "Datenträger konnten nicht gelesen werden: " + e.message; }\n'
    '  S.scanning = false;\n'
    '  $("b-refresh").disabled = false;\n'
    '  renderDisks(); updateSel();\n'
    '}\n'
    '\n'
    'function renderModes() {\n'
    '  const box = $("modes");\n'
    '  box.replaceChildren();\n'
    '  S.info.modes.forEach((m, i) => box.append(h("button", { type: "button", class: "mode" + (i === S.mode ? " on" : ""), role: "radio", "aria-checked": String(i === S.mode), onclick: () => { S.mode = i; renderModes(); } },\n'
    '    h("b", {}, m.name), h("span", { class: "sub" }, m.sub), h("span", { class: "info" }, m.info))));\n'
    '}\n'
    '\n'
    '// ───── Bestätigung und Start ─────\n'
    'function confirmDialog(plan) {\n'
    '  const sel = selected(), n = sel.length, word = n === 1 ? sel[0].id : String(n), mode = S.info.modes[S.mode];\n'
    '  const planText = (x) => {\n'
    '    const it = plan.find((p) => p.id === x.id), f = it && x.setup.fs.find((q) => q.key === it.fs);\n'
    '    return it ? `Danach: als neues Laufwerk einrichten – ${it.scheme.toUpperCase()}, ${f ? f.name : it.fs}, Name „${it.label}“` : "Danach: nicht einrichten (der Datenträger bleibt leer und im System unsichtbar)";\n'
    '  };\n'
    '  const verify = $("o-verify").checked, par = n > 1 && $("o-flow").value === "true", d = $("d-confirm");\n'
    '  const input = h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Bestätigung" });\n'
    '  const ok = h("button", { class: "btn danger", type: "button", disabled: true }, "Endgültig löschen");\n'
    '  input.addEventListener("input", () => { ok.disabled = input.value.trim().toLowerCase() !== word.toLowerCase(); });\n'
    '  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !ok.disabled) ok.click(); });\n'
    '  ok.addEventListener("click", async () => {\n'
    '    ok.disabled = true;\n'
    '    try {\n'
    '      await api("start", { ids: sel.map((x) => x.id), mode: S.mode, verify, threads: Number($("o-perf").value), parallel: par, confirm: input.value, setup: plan });\n'
    '      d.close();\n'
    '      S.view = { state: "launching" };\n'
    '      showRun();\n'
    '    } catch (e) { msg("Start nicht möglich", e.message, "OK"); ok.disabled = false; }\n'
    '  });\n'
    '  d.replaceChildren(h("div", { class: "dlg" },\n'
    '    h("h3", { class: "red" }, n === 1 ? "Datenträger endgültig löschen?" : `${n} Datenträger endgültig löschen?`),\n'
    '    h("div", { class: "box" }, sel.flatMap((x) => [h("b", {}, `${x.id}  ·  ${x.model || "Unbekanntes Modell"}  ·  ${x.size_h}`), h("small", {}, "Inhalt: " + (x.volumes.join(", ") || "keine erkennbaren Laufwerke")), h("small", {}, planText(x))]),\n'
    '      h("small", {}, `Methode: ${mode.name} (${mode.passes.join(" → ")})` + (verify ? "  +  Prüfung" : "") + (n > 1 ? `  ·  ${par ? "gleichzeitig" : "nacheinander"}` : ""))),\n'
    '    h("p", {}, (n === 1 ? "Alle Daten gehen unwiderruflich verloren. " : `Alle Daten auf diesen ${n} Datenträgern gehen unwiderruflich verloren. `)\n'
    '      + (n === 1 ? `Zur Bestätigung „${word}“ eingeben:` : `Zur Bestätigung die Anzahl „${word}“ eingeben:`)),\n'
    '    input,\n'
    '    h("div", { class: "btns" }, h("button", { class: "btn", type: "button", onclick: () => d.close() }, "Abbrechen"), ok)));\n'
    '  d.showModal();\n'
    '  input.focus();\n'
    '}\n'
    '\n'
    '// ───── Fortschritt ─────\n'
    'function showSelect() {\n'
    '  $("v-run").hidden = true; $("v-select").hidden = false; $("bar").hidden = false; document.title = TITLE;\n'
    '}\n'
    '\n'
    'function showRun() {\n'
    '  $("v-select").hidden = true; $("v-run").hidden = false; $("bar").hidden = true;\n'
    '  pollLoop();\n'
    '}\n'
    '\n'
    'function drawChart(hist) {\n'
    '  const c = $("r-chart"), dpr = window.devicePixelRatio || 1, w = c.clientWidth, hh = c.clientHeight;\n'
    '  if (!w || !hh) return;\n'
    '  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(hh * dpr); }\n'
    '  const x = c.getContext("2d"), st = getComputedStyle(document.documentElement);\n'
    '  x.setTransform(dpr, 0, 0, dpr, 0, 0);\n'
    '  x.clearRect(0, 0, w, hh);\n'
    '  if (hist.length < 2) return;\n'
    '  const top = (Math.max(...hist) * 1.15) || 1, pts = hist.map((v, i) => [(i / (hist.length - 1)) * w, hh - 2 - (v / top) * (hh - 8)]);\n'
    '  x.beginPath(); x.moveTo(0, hh); pts.forEach((p) => x.lineTo(p[0], p[1])); x.lineTo(w, hh); x.closePath();\n'
    '  x.fillStyle = st.getPropertyValue("--chart"); x.fill();\n'
    '  x.beginPath(); pts.forEach((p, i) => (i ? x.lineTo(p[0], p[1]) : x.moveTo(p[0], p[1])));\n'
    '  x.strokeStyle = st.getPropertyValue("--acc"); x.lineWidth = 2; x.stroke();\n'
    '  x.fillStyle = st.getPropertyValue("--mute"); x.font = "12px sans-serif";\n'
    '  x.fillText("max. " + human(top / 1.15, 0) + "/s", 8, 16);\n'
    '}\n'
    '\n'
    'function renderRun(v) {\n'
    '  $("r-title").textContent = v.title || "";\n'
    '  $("r-phase").textContent = v.phase || "";\n'
    '  $("r-pct").textContent = v.pct || ""; $("r-pct").dataset.c = v.col || "acc";\n'
    '  $("r-meter").dataset.c = v.col || "acc"; $("r-meter").firstElementChild.style.setProperty("--v", v.frac || 0);\n'
    '  for (const k of ["speed", "avg", "el", "eta", "done"]) $("s-" + k).textContent = (v.stats && v.stats[k]) || "–";\n'
    '  const rows = $("r-rows"), top = rows.scrollTop;\n'
    '  rows.hidden = !v.multi;\n'
    '  if (v.multi) {\n'
    '    rows.replaceChildren(...v.rows.map((r) => {\n'
    '      const bar = h("div", { class: "bar-s", "data-c": r.col }, h("i"));\n'
    '      bar.firstElementChild.style.setProperty("--v", r.frac);\n'
    '      return h("div", { class: "rowd", "data-c": r.col }, h("div", { class: "nm" }, r.id, h("span", {}, r.sub)), h("div", { class: "pc" }, r.pct), bar,\n'
    '        h("div", { class: "ph" }, r.phase), h("div", { class: "rt" }, r.rate));\n'
    '    }));\n'
    '    rows.scrollTop = top;\n'
    '  }\n'
    '  const fin = v.state === "finished";\n'
    '  $("b-cancel").hidden = fin; $("b-cancel").disabled = !v.can_cancel;\n'
    '  $("r-result").hidden = !fin;\n'
    '  if (fin) { $("r-banner").className = "banner " + v.result.kind; $("r-banner").textContent = v.result.text; }\n'
    '  $("r-note").hidden = !v.note; $("r-note").textContent = v.note || "";\n'
    '  drawChart(v.hist || []);\n'
    '  document.title = fin || !v.frac ? TITLE : `Schredder – ${Math.round(v.frac * 100)} %`;\n'
    '}\n'
    '\n'
    'async function pollLoop() {\n'
    '  if (S.looping) return;\n'
    '  S.looping = true;\n'
    '  try {\n'
    '    while (S.view && S.view.state !== "idle") {\n'
    '      try { S.view = await api("run"); } catch (e) { offline(); break; }\n'
    '      renderRun(S.view);\n'
    '      if (S.view.state === "ask") await askSetup(S.view.ask);\n'
    '      if (S.view.state === "finished") break;\n'
    '      await sleep(500);\n'
    '    }\n'
    '  } finally { S.looping = false; }\n'
    '}\n'
    '\n'
    '// ───── Einrichtung als Laufwerk ─────\n'
    '// Dialog mit Format-Vorschlag je Datenträger; liefert die Wahl (Liste) oder o.decline / o.esc\n'
    'function pickSetup(items, o) {\n'
    '  return new Promise((resolve) => {\n'
    '    const d = $("d-setup");\n'
    '    let done = false;\n'
    '    const rows = items.map((it) => {\n'
    '      const on = h("input", { type: "checkbox" }), fs = h("select"), sch = h("select"), label = h("input", { type: "text", size: "14", maxlength: "32", "aria-label": "Name" });\n'
    '      const why = h("div", { class: "why" });\n'
    '      on.checked = true; label.value = it.label;\n'
    '      fs.append(...it.fs.map((f) => h("option", { value: f.key }, f.name)));\n'
    '      fs.value = it.rec.fs;\n'
    '      let cur = it.rec.scheme;\n'
    '      const refresh = () => {\n'
    '        const opts = it.schemes[fs.value];\n'
    '        sch.replaceChildren(...opts.map((s) => h("option", { value: s }, s.toUpperCase())));\n'
    '        sch.value = opts.includes(cur) ? cur : opts[0];\n'
    '        sch.disabled = opts.length < 2;\n'
    '        cur = sch.value;\n'
    '        why.textContent = (sch.value === it.rec.scheme && fs.value === it.rec.fs ? "Empfohlen: " : "Gewählt: ") + it.why[`${sch.value}|${fs.value}`];\n'
    '      };\n'
    '      fs.addEventListener("change", refresh); sch.addEventListener("change", () => { cur = sch.value; refresh(); });\n'
    '      refresh();\n'
    '      return { id: it.id, on, fs, sch, label,\n'
    '        el: h("div", { class: "sdisk" }, h("label", {}, on, `${it.id}  ·  ${it.model}  ·  ${it.size_h}`),\n'
    '          h("div", { class: "ctl" }, h("label", {}, "Partitionstabelle", sch), h("label", {}, "Dateisystem", fs), h("label", {}, "Name", label)), why) };\n'
    '    });\n'
    '    const finish = (v) => {\n'
    '      if (done) return;\n'
    '      done = true; d.close(); resolve(v);\n'
    '    };\n'
    '    d.addEventListener("close", () => finish(o.esc), { once: true }); // Esc oder Schließen\n'
    '    d.replaceChildren(h("div", { class: "dlg" }, h("h3", {}, o.title), h("p", { class: "mute" }, o.intro), rows.map((r) => r.el),\n'
    '      h("div", { class: "btns" }, h("button", { class: "btn", type: "button", onclick: () => finish(o.decline) }, o.no),\n'
    '        h("button", { class: "btn primary", type: "button", onclick: () => finish(rows.filter((r) => r.on.checked).map((r) => ({ id: r.id, scheme: r.sch.value, fs: r.fs.value, label: r.label.value }))) }, o.ok))));\n'
    '    d.showModal();\n'
    '  });\n'
    '}\n'
    '\n'
    '// Rückfrage nach dem Löschen – nur noch, wenn die Einrichtung nicht schon vor dem Start festgelegt wurde\n'
    'async function askSetup(items) {\n'
    '  const list = await pickSetup(items, { title: "Als neues Laufwerk einrichten?", ok: "Einrichten", no: "Nicht einrichten", decline: [], esc: [],\n'
    '    intro: "Gelöschte Datenträger sind leer und im System nicht mehr als Laufwerk sichtbar. Schredder kann sie jetzt neu einrichten (Partitionstabelle und Dateisystem). Das passende Format wurde nach Art und Größe des Datenträgers und nach Betriebssystem erkannt; es lässt sich ändern." });\n'
    '  try { await api("setup", { items: list }); } catch (e) { msg("Einrichtung nicht möglich", e.message, "OK"); }\n'
    '}\n'
    '\n'
    '// Einrichtung vor dem Start festlegen: null = Vorgang abbrechen\n'
    'async function planSetup(sel) {\n'
    '  const how = $("o-setup").value;\n'
    '  if (how === "none") return [];\n'
    '  if (how === "auto") return sel.map((d) => ({ id: d.id, scheme: d.setup.rec.scheme, fs: d.setup.rec.fs, label: d.setup.label }));\n'
    '  return pickSetup(sel.map((d) => d.setup), { title: "Format für die neuen Laufwerke wählen", ok: "Weiter", no: "Abbrechen", decline: null, esc: null,\n'
    '    intro: "Nach dem Löschen richtet Schredder die Datenträger so ein. Die Vorschläge richten sich nach Art, Größe und Betriebssystem; abgewählte Datenträger bleiben leer." });\n'
    '}\n'
    '\n'
    '// ───── Protokoll, Verbindung, Start ─────\n'
    'async function saveReport() {\n'
    '  try {\n'
    '    const r = await fetch("/api/report", { headers: { "X-Schredder-Token": TOKEN } });\n'
    '    if (!r.ok) throw new Error("HTTP " + r.status);\n'
    '    const t = await r.text(), n = new Date(), p = (x) => String(x).padStart(2, "0");\n'
    '    const a = h("a", { href: URL.createObjectURL(new Blob([t], { type: "text/plain;charset=utf-8" })), download: `Schredder-Protokoll-${n.getFullYear()}${p(n.getMonth() + 1)}${p(n.getDate())}-${p(n.getHours())}${p(n.getMinutes())}.txt` });\n'
    '    document.body.append(a); a.click(); a.remove();\n'
    '  } catch (e) { msg("Protokoll", "Das Protokoll konnte nicht gespeichert werden: " + e.message, "OK"); }\n'
    '}\n'
    '\n'
    'function offline() {\n'
    '  S.offline = true; $("offline").hidden = false; updateSel();\n'
    '}\n'
    '\n'
    'async function init() {\n'
    '  if (!TOKEN) { $("offline").textContent = "Bitte Schredder über die Startdatei (Schredder.bat, Schredder.command oder Schredder.sh) starten."; $("offline").hidden = false; return; }\n'
    '  try { S.info = await api("state"); } catch (e) { return offline(); }\n'
    '  const i = S.info;\n'
    '  $("ver").textContent = `Version ${i.version}`;\n'
    '  $("rights").textContent = i.admin ? "Administratorrechte: vorhanden" : "Administratorrechte: werden beim Löschen angefordert";\n'
    '  $("about").textContent = `Schredder ${i.version} · Python ${i.python} · ${i.system}`;\n'
    '  $("o-flow").append(...i.flow.map((f) => h("option", { value: String(f.value) }, f.name)));\n'
    '  $("o-perf").append(...i.perf.map((f) => h("option", { value: String(f.value) }, f.name)));\n'
    '  renderModes();\n'
    '  $("b-all").addEventListener("click", () => { S.sel = new Set(S.disks.filter((d) => !d.locked).map((d) => d.id)); S.peek = null; renderDisks(); updateSel(); });\n'
    '  $("b-refresh").addEventListener("click", scan);\n'
    '  $("b-go").addEventListener("click", async () => {\n'
    '    const plan = await planSetup(selected());\n'
    '    if (plan) confirmDialog(plan);\n'
    '  });\n'
    '  $("b-cancel").addEventListener("click", async () => {\n'
    '    if (!(await msg("Löschvorgang abbrechen?", "Bereits begonnene Datenträger bleiben teilweise überschrieben.", "Ja, abbrechen", "Weiter löschen"))) return;\n'
    '    try { await api("cancel", {}); } catch (e) { msg("Abbrechen nicht möglich", e.message, "OK"); }\n'
    '  });\n'
    '  $("b-report").addEventListener("click", saveReport);\n'
    '  $("b-back").addEventListener("click", async () => {\n'
    '    try { await api("back", {}); } catch (e) { return msg("Zurück nicht möglich", e.message, "OK"); }\n'
    '    S.view = null; showSelect(); scan();\n'
    '  });\n'
    '  setInterval(() => api("ping", {}).catch(offline), 3000);\n'
    '  addEventListener("pagehide", () => { fetch("/api/bye", { method: "POST", keepalive: true, headers: { "X-Schredder-Token": TOKEN }, body: "{}" }).catch(() => {}); });\n'
    '  addEventListener("beforeunload", (e) => { if (active()) { e.preventDefault(); e.returnValue = ""; } });\n'
    '  if (i.run) { S.view = i.run; showRun(); } else { showSelect(); scan(); }\n'
    '  updateSel();\n'
    '}\n'
    'init();\n'
)


if __name__ == "__main__":
    main()
