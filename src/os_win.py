
# ───────────────────────── Windows ─────────────────────────
import base64
from ctypes import wintypes

PLATFORM = "win"
PREPARE_DESTROYS = True  # prepare() entfernt die Partitionen
NOWIN = 0x08000000  # CREATE_NO_WINDOW
_INVALID = ctypes.c_void_p(-1).value
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.SetFilePointerEx.argtypes = (ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD)
_k32.SetFilePointerEx.restype = wintypes.BOOL
for _f in (_k32.WriteFile, _k32.ReadFile):
    _f.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _f.restype = wintypes.BOOL
_k32.FlushFileBuffers.argtypes = (ctypes.c_void_p,)
_k32.CloseHandle.argtypes = (ctypes.c_void_p,)


def _ps_cmd(script):
    pre = "$ProgressPreference='SilentlyContinue';try{[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)}catch{};"
    enc = base64.b64encode((pre + script).encode("utf-16-le")).decode()
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text", "-EncodedCommand", enc]


def _ps(script, timeout=180):
    """PowerShell mit festem UTF-8-Ausgabeformat; Ausgabe als Bytes holen und selbst dekodieren
    (die Konsolen-Codepage kennt keine Umlaute zuverlässig)."""
    p = subprocess.run(_ps_cmd(script), capture_output=True, stdin=subprocess.DEVNULL, creationflags=NOWIN, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


_LIST = r'''
$ErrorActionPreference = 'SilentlyContinue'
$pd = @{}
Get-PhysicalDisk | ForEach-Object { $pd[[string]$_.DeviceId] = $_ }
$r = Get-Disk | ForEach-Object {
  $n = $_.Number
  $v = @(Get-Partition -DiskNumber $n | Get-Volume | Where-Object { $_.DriveLetter } | ForEach-Object {
    if ($_.FileSystemLabel) { "$($_.DriveLetter): ($($_.FileSystemLabel))" } else { "$($_.DriveLetter):" } })
  [pscustomobject]@{ n = [int]$n; name = [string]$_.FriendlyName; serial = [string]$_.SerialNumber; size = [int64]$_.Size
    sector = [int]$_.LogicalSectorSize; bus = [string]$_.BusType; sys = [bool]($_.IsSystem -or $_.IsBoot)
    media = [string]$pd[[string]$n].MediaType; vol = $v; parts = @(Get-Partition -DiskNumber $n).Count }
}
ConvertTo-Json -InputObject @($r) -Depth 4 -Compress
'''


def list_disks():
    rc, out, err = _ps(_LIST)
    if rc or "[" not in out:
        raise RuntimeError("Datenträgerliste nicht lesbar: " + (err or out).strip()[:300])
    data = json.loads(out[out.index("["):])
    here = os.path.splitdrive(SCRIPT)[0].upper().rstrip(":")
    res = []
    for d in data:
        if not d or d["bus"] in ("File Backed Virtual", "Spaces", "Storage Spaces"):
            continue
        vols = d["vol"] if isinstance(d["vol"], list) else [d["vol"]] if d["vol"] else []
        bus, media = d["bus"], d["media"]
        kind = ("NVMe" if bus == "NVMe" else "SSD" if media == "SSD" else "HDD" if media == "HDD" else
                "Flash" if bus in ("USB", "SD", "MMC") else "?")
        locked = ("Systemplatte (Windows)" if d["sys"] else
                  "Enthält dieses Programm" if here and here in {v[0].upper() for v in vols} else "")
        res.append(dict(id=f"Disk {d['n']}", n=d["n"], path=rf"\\.\PhysicalDrive{d['n']}", size=int(d["size"]), model=d["name"].strip(),
                        serial=(d["serial"] or "").strip(), bus=bus, kind=kind, sector=int(d["sector"] or 512), locked=locked,
                        volumes=vols or ([f"{d['parts']} Partition(en) ohne Laufwerksbuchstabe"] if d.get("parts") else [])))
    return res


def preflight(disk):
    """Nicht zerstörend, vor dem Entfernen der Partitionen: Datenträger online/beschreibbar schalten, Zugriff und
    Schreibweg testen (erster Sektor wird unverändert zurückgeschrieben). Scheitert etwas, bleibt alles wie es war."""
    n = int(disk["n"])
    rc, out, err = _ps(f"""$ErrorActionPreference='Stop'
try {{
  $d = Get-Disk -Number {n}
  if ($d.IsSystem -or $d.IsBoot) {{ throw 'Systemdatenträger' }}
  if ($d.IsOffline) {{ Set-Disk -Number {n} -IsOffline $false }}
  if ($d.IsReadOnly) {{ Set-Disk -Number {n} -IsReadOnly $false }}
}} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}""")
    if rc:
        raise WipeError("Datenträger nicht verfügbar: " + (err or out).strip()[:300])
    try:
        dev = WinDev(disk["path"])
    except OSError as e:
        raise WipeError(f"Kein Zugriff auf den Datenträger ({explain(e)}). Läuft Schredder mit Administratorrechten?") from e
    try:
        buf, n = Buf(CHUNK), max(512, int(disk.get("sector") or 512))
        dev.read(buf, n, 0)
        try:
            dev.write(buf, n, 0)
        except OSError as e:
            if getattr(e, "winerror", None) != 5:  # 5: Windows schützt eingehängte Volumes – hier unerheblich
                raise WipeError(f"Schreibtest fehlgeschlagen: {explain(e)}") from e
    except OSError as e:
        raise WipeError(f"Lesetest fehlgeschlagen: {explain(e)}") from e
    finally:
        dev.close()


def prepare(disk):
    """Partitionen entfernen (Clear-Disk) – sonst sperrt Windows den Rohzugriff auf eingehängte Bereiche."""
    n = int(disk["n"])
    rc, out, err = _ps(f"""$ErrorActionPreference='Stop'
try {{
  $d = Get-Disk -Number {n}
  if ($d.IsSystem -or $d.IsBoot) {{ throw 'Systemdatenträger' }}
  if ($d.PartitionStyle -ne 'RAW') {{ Clear-Disk -Number {n} -RemoveData -RemoveOEM -Confirm:$false }}
}} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}""")
    if rc:
        raise WipeError("Partitionen konnten nicht entfernt werden: " + (err or out).strip()[:400])


def setup_fs(d):
    return ["ntfs", "exfat"] + (["fat32"] if d["size"] <= FAT32_MAX else [])


def native_fs(d):
    return "ntfs"


def setup_disk(disk, scheme, fs, label, owner):
    """Partitionstabelle anlegen, eine Partition über den ganzen Datenträger, schnell formatieren, Laufwerksbuchstaben vergeben."""
    n = int(disk["n"])
    mbr = f"-MbrType {'FAT32' if fs == 'fat32' else 'IFS'}" if scheme == "mbr" else ""
    rc, out, err = _ps(f"""$ErrorActionPreference='Stop'
try {{
  Update-Disk -Number {n}
  $d = Get-Disk -Number {n}
  if ($d.IsSystem -or $d.IsBoot) {{ throw 'Systemdatenträger' }}
  if ($d.IsOffline) {{ Set-Disk -Number {n} -IsOffline $false }}
  if ($d.IsReadOnly) {{ Set-Disk -Number {n} -IsReadOnly $false }}
  if ($d.PartitionStyle -ne 'RAW') {{ Clear-Disk -Number {n} -RemoveData -RemoveOEM -Confirm:$false }}
  Initialize-Disk -Number {n} -PartitionStyle {scheme.upper()}
  $p = New-Partition -DiskNumber {n} -UseMaximumSize -AssignDriveLetter {mbr}
  $null = Format-Volume -Partition $p -FileSystem {FS_NAME[fs]} -NewFileSystemLabel '{label}' -Confirm:$false
  Start-Sleep -Seconds 1
  $p = Get-Partition -DiskNumber {n} | Sort-Object Size -Descending | Select-Object -First 1
  if (-not $p.DriveLetter) {{
    Add-PartitionAccessPath -DiskNumber {n} -PartitionNumber $p.PartitionNumber -AssignDriveLetter
    Start-Sleep -Seconds 1
    $p = Get-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber
  }}
  $v = $p | Get-Volume
  if (-not $p.DriveLetter -or $v.FileSystemType -ne '{FS_NAME[fs]}') {{ throw 'Das neue Laufwerk ist nicht verfügbar (kein Laufwerksbuchstabe oder Dateisystem).' }}
  [string]$p.DriveLetter
}} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}""", timeout=900)
    if rc:
        raise WipeError("Einrichten fehlgeschlagen: " + (err or out).strip()[:400])
    letter = (out.strip().splitlines() or [""])[-1].strip()
    if len(letter) != 1 or not letter.isalpha():
        raise WipeError("Einrichten fehlgeschlagen: Das neue Laufwerk hat keinen Laufwerksbuchstaben.")
    return letter.upper() + ":"


def after_setup(info):
    return info  # Windows vergibt den Laufwerksbuchstaben selbst


class WinDev:
    """Ungepuffertes Schreiben (FILE_FLAG_NO_BUFFERING): ein Handle je Thread, damit die Anfragen parallel laufen."""

    def __init__(self, path):
        self.path, self.flags, self.tl, self.handles, self.lock = path, 0x20000000, threading.local(), [], threading.Lock()
        self._h()  # Fehler (Zugriff verweigert …) sofort im Hauptthread

    def _open(self):
        for attempt in range(20):
            h = _k32.CreateFileW(self.path, 0xC0000000, 3, None, 3, self.flags, None)  # lesen+schreiben, teilen, OPEN_EXISTING
            if h not in (None, _INVALID):
                return h
            err = ctypes.get_last_error()
            if err in (5, 32) and attempt < 19:  # Dienste (Suche, Virenscanner) halten das Gerät kurz nach dem Löschen der Partitionen
                time.sleep(0.5)
            elif err == 87 and self.flags:       # NO_BUFFERING nicht unterstützt → gepuffert weiter
                self.flags = 0
            else:
                raise ctypes.WinError(err)

    def _h(self):
        h = getattr(self.tl, "h", None)
        if h is None:
            h = self.tl.h = self._open()
            with self.lock:
                self.handles.append(h)
        return h

    def _io(self, fn, buf, n, off):
        h = self._h()
        if not _k32.SetFilePointerEx(h, off, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        done, pos = wintypes.DWORD(0), 0
        while pos < n:
            if not fn(h, buf.addr + pos, n - pos, ctypes.byref(done), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not done.value:
                raise OSError(errno.EIO, "Ein-/Ausgabe lieferte 0 Bytes")
            pos += done.value

    def write(self, buf, n, off):
        self._io(_k32.WriteFile, buf, n, off)

    def read(self, buf, n, off):
        self._io(_k32.ReadFile, buf, n, off)

    def flush(self):
        _k32.FlushFileBuffers(self._h())  # Schreibcache des Geräts leeren

    def close(self):
        for h in self.handles:
            _k32.CloseHandle(h)


def open_dev(path):
    return WinDev(path)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def launch_worker(job):
    if is_admin():
        return subprocess.Popen([sys.executable, os.path.join(os.path.dirname(job), "worker.py"), "--worker", job], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=NOWIN)
    q = lambda t: "'" + t.replace("'", "''") + "'"
    ps = ("try { $p = Start-Process -FilePath %s -ArgumentList %s -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode } "
          "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 9 }") % (q(sys.executable), q(f'"{os.path.join(os.path.dirname(job), "worker.py")}" --worker "{job}"'))
    return subprocess.Popen(_ps_cmd(ps), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)


def inhibit_sleep():
    try:
        _k32.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
        _k32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, endet mit dem Worker
    except Exception:
        pass  # nur Komfort: das Löschen läuft auch ohne Ruhezustand-Sperre
