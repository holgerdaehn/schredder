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
