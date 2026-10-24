
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
