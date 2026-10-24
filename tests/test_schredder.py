"""Tests für Engine, Worker und Weboberfläche (HTTP-Schnittstelle). Sie laufen auf Dateien statt echten Datenträgern und auf jedem System:

    python3 tests/test_schredder.py -v

Geprüft wird der Build des aktuellen Systems (SCHREDDER_TARGET=win|macos|linux wählt einen anderen).
"""
import errno
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE = {"win32": "win", "darwin": "macos"}.get(sys.platform, "linux")
TARGET = os.environ.get("SCHREDDER_TARGET") or NATIVE
spec = importlib.util.spec_from_file_location("schredder", os.path.join(ROOT, TARGET, "schredder.py"))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
ORIG = {k: getattr(s, k) for k in ("ALIVE_POLLS", "is_admin", "prepare", "preflight", "open_dev", "write_pass", "list_disks", "inhibit_sleep", "setup_disk", "SETUP_WAIT",
                                    "launch_worker", "after_setup", "SCRIPT")}
KB = 4096  # alle Größen: Vielfache von 4 KiB (ungepufferte E/A verlangt Sektorgrenzen)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp, self.calls = tempfile.mkdtemp(), []
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for k, v in ORIG.items():
            setattr(s, k, v)
        s.is_admin = lambda: True
        s.inhibit_sleep = lambda: None
        s.preflight = lambda d: self.calls.append(("preflight", d["id"]))
        s.prepare = lambda d: self.calls.append(("prepare", d["id"]))

    def disks(self, *sizes):
        out = []
        for k, size in enumerate(sizes):
            p = os.path.join(self.tmp, f"disk{k}.img")
            with open(p, "wb") as f:
                f.write(os.urandom(size))
            out.append(dict(id=f"d{k}", path=p, size=size, model=f"M{k}", serial=f"S{k}", bus="USB", kind="SSD", sector=512,
                            volumes=[], locked=""))
        return out

    def run_job(self, ds, passes=("zero",), verify=True, threads=2, parallel=True, listing=None, setup=False):
        s.list_disks = lambda: ds if listing is None else listing
        job = dict(script="x", disks=[{k: d[k] for k in ("id", "path", "size", "model", "serial")} for d in ds], passes=list(passes),
                   verify=verify, threads=threads, parallel=parallel, status=os.path.join(self.tmp, "status.json"),
                   cancel=os.path.join(self.tmp, "cancel"))
        if setup:  # Einrichtung als Laufwerk anbieten (wartet auf setup.json)
            job["setup_file"] = os.path.join(self.tmp, "setup.json")
            if setup == "alive":  # Oberfläche meldet sich laufend (Datei alive) – wie im echten Betrieb
                job["alive_file"] = os.path.join(self.tmp, "alive")
                open(job["alive_file"], "w").close()
        jf = os.path.join(self.tmp, "job.json")
        with open(jf, "w") as f:
            json.dump(job, f)
        rc = s.worker(jf)
        with open(job["status"]) as f:
            return rc, json.load(f)

    @staticmethod
    def data(path):
        with open(path, "rb") as f:
            return f.read()


class Overwrite(Base):
    def test_all_methods_one_and_many_disks(self):
        for parallel in (True, False):
            for passes, verify in ((("zero",), True), (("random", "zero"), True), (("random",) * 3 + ("zero",), False)):
                ds = self.disks(3 * KB, 5 << 20, (9 << 20) + KB)
                rc, st = self.run_job(ds, passes, verify, 3, parallel)
                self.assertEqual((rc, st["state"]), (0, "done"), st)
                for d in ds:
                    x = st["disks"][d["id"]]
                    self.assertEqual(x["state"], "done")
                    self.assertEqual(self.data(d["path"]), bytes(d["size"]))
                    self.assertEqual(x["done"], d["size"] * (len(passes) + (verify and passes[-1] == "zero")))

    def test_random_pass_looks_random(self):
        (d,) = self.disks(8 << 20)
        rc, st = self.run_job([d], ("random",), True)
        blocks = {self.data(d["path"])[i:i + KB] for i in range(0, d["size"], KB)}
        self.assertEqual(st["disks"]["d0"]["state"], "done")
        self.assertFalse(st["disks"]["d0"]["verified"])  # Verifikation nur nach einem Nullen-Durchgang
        self.assertEqual(len(blocks), d["size"] // KB)

    def test_verify_detects_leftover(self):
        (d,) = self.disks(8 << 20)
        with open(d["path"], "wb") as f:
            f.write(bytes(d["size"]))
        with open(d["path"], "r+b") as f:
            f.seek(5 << 20)
            f.write(b"\x01")
        dev = s.open_dev(d["path"])
        try:
            with self.assertRaises(s.WipeError) as cm:
                s.verify_pass(dev, d["size"], 2, s.Counter(), lambda: False)
        finally:
            dev.close()
        self.assertIn("Prüfung fehlgeschlagen", str(cm.exception))


class Safety(Base):
    def test_without_rights_nothing_happens(self):
        ds = self.disks(1 << 20, 1 << 20)
        before = [self.data(d["path"]) for d in ds]
        s.is_admin = lambda: False
        rc, st = self.run_job(ds)
        self.assertEqual((rc, st["state"]), (1, "error"))
        self.assertIn("Rechte", st["error"])
        self.assertEqual(self.calls, [])  # weder geprüft noch vorbereitet
        self.assertEqual([self.data(d["path"]) for d in ds], before)
        self.assertTrue(all(x["state"] == "wait" and not x.get("modified") for x in st["disks"].values()))

    def test_failed_preflight_stops_only_that_disk(self):
        ds = self.disks(2 << 20, 2 << 20)
        before = self.data(ds[1]["path"])

        def pf(d):
            self.calls.append(("preflight", d["id"]))
            if d["id"] == "d1":
                raise OSError(errno.EACCES, "Permission denied")
        s.preflight = pf
        rc, st = self.run_job(ds)
        self.assertEqual(st["disks"]["d0"]["state"], "done")
        x = st["disks"]["d1"]
        self.assertEqual((x["state"], x["modified"]), ("error", False))
        self.assertNotIn(("prepare", "d1"), self.calls)
        self.assertEqual(self.data(ds[1]["path"]), before)
        self.assertEqual(s.data_note(x), "Es wurde nichts verändert.")

    def test_changed_locked_or_missing_disks_are_skipped(self):
        ds = self.disks(1 << 20, 1 << 20, 1 << 20, 1 << 20)
        listing = [dict(ds[0]), dict(ds[1], locked="Systemplatte"), dict(ds[2], serial="ANDERS")]  # d3 fehlt
        rc, st = self.run_job(ds, listing=listing)
        self.assertEqual([st["disks"][d["id"]]["state"] for d in ds], ["done", "error", "error", "error"])
        self.assertEqual({c[1] for c in self.calls}, {"d0"})

    def test_failure_after_prepare_reports_the_state(self):
        (d,) = self.disks(1 << 20)

        def boom(path):
            raise OSError(errno.EIO, "I/O error")
        s.open_dev = boom
        rc, st = self.run_job([d])
        x = st["disks"]["d0"]
        self.assertEqual((x["state"], x["modified"]), ("error", s.PREPARE_DESTROYS))
        self.assertEqual("Partitionen" in s.data_note(x), s.PREPARE_DESTROYS)


class Flow(Base):
    def concurrency(self, parallel):
        ds, act, lock = self.disks(*[3 << 20] * 4), {"n": 0, "max": 0}, threading.Lock()

        def wp(*a, **k):
            with lock:
                act["n"] += 1
                act["max"] = max(act["max"], act["n"])
            time.sleep(0.15)
            try:
                return ORIG["write_pass"](*a, **k)
            finally:
                with lock:
                    act["n"] -= 1
        s.write_pass = wp
        self.assertEqual(self.run_job(ds, parallel=parallel)[1]["state"], "done")
        return act["max"]

    def test_parallel_and_sequential(self):
        self.assertEqual(self.concurrency(True), 4)
        self.assertEqual(self.concurrency(False), 1)

    def test_cancel_stops_before_next_disk_is_touched(self):
        ds = self.disks(4 << 20, 4 << 20, 4 << 20)
        before = [self.data(d["path"]) for d in ds]

        def pf(d):
            self.calls.append(("preflight", d["id"]))
            if d["id"] == "d1":  # Abbruch genau beim Start des zweiten Datenträgers
                open(os.path.join(self.tmp, "cancel"), "w").close()
        s.preflight = pf
        rc, st = self.run_job(ds, parallel=False)
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual([st["disks"][d["id"]]["state"] for d in ds], ["done", "cancelled", "cancelled"])
        self.assertNotIn(("prepare", "d1"), self.calls)
        self.assertNotIn(("preflight", "d2"), self.calls)
        self.assertEqual([self.data(d["path"]) for d in ds[1:]], before[1:])


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.setups = []
        s.setup_disk = lambda d, scheme, fs, label, owner: (self.setups.append((d["id"], scheme, fs, label)), "Ziel-" + d["id"])[1]
        s.SETUP_WAIT = 20

    def gui(self, answer):
        """Spielt die Oberfläche: wartet auf state=ask, merkt sich die angebotenen Datenträger und antwortet."""
        status, seen = os.path.join(self.tmp, "status.json"), {}

        def play():
            end = time.time() + 20
            while time.time() < end:
                try:
                    with open(status) as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    st = {}
                if st.get("state") == "ask":
                    seen["ask"] = st["ask"]
                    return answer()
                time.sleep(0.02)
        t = threading.Thread(target=play)
        t.start()
        self.addCleanup(t.join)
        return seen

    def answer(self, items):
        with open(os.path.join(self.tmp, "setup.json"), "w") as f:
            json.dump({"setup": items}, f)

    def test_offers_only_wiped_disks_and_runs_only_valid_choices(self):
        ds, fs = self.disks(2 << 20, 2 << 20, 2 << 20), s.setup_fs({"size": 2 << 20, "kind": "Flash", "bus": "USB"})[0]

        def pf(d):
            if d["id"] == "d1":
                raise OSError(errno.EACCES, "Permission denied")
        s.preflight = pf
        seen = self.gui(lambda: self.answer([dict(id="d0", scheme="gpt", fs=fs, label="Mein Stick!"), dict(id="d1", scheme="gpt", fs=fs, label="x"),
                                             dict(id="d2", scheme="gpt", fs="reiserfs", label="y")]))
        rc, st = self.run_job(ds, setup=True)
        self.assertEqual(seen["ask"], ["d0", "d2"])  # d1 wurde nicht gelöscht, also nicht angeboten
        self.assertEqual(self.setups, [("d0", "gpt", fs, s.clean_label(fs, "Mein Stick!"))])
        self.assertEqual((st["state"], st["disks"]["d0"]["setup"], st["disks"]["d2"]["setup"]), ("done", "done", "error"))
        self.assertEqual(st["disks"]["d0"]["setup_info"], "Ziel-d0")
        self.assertNotIn("setup", st["disks"]["d1"])

    def test_decline_does_nothing(self):
        self.gui(lambda: self.answer([]))
        rc, st = self.run_job(self.disks(1 << 20), setup=True)
        self.assertEqual((st["state"], self.setups), ("done", []))
        self.assertNotIn("setup", st["disks"]["d0"])

    def test_no_answer_times_out(self):
        s.SETUP_WAIT = 0.5
        rc, st = self.run_job(self.disks(1 << 20), setup=True)
        self.assertEqual((rc, st["state"], self.setups), (0, "done", []))

    def test_waits_as_long_as_the_interface_lives(self):
        s.SETUP_WAIT = 0.5  # ohne Lebenszeichen wäre nach 0,5 s Schluss
        alive, stop = os.path.join(self.tmp, "alive"), threading.Event()
        fs = s.setup_fs({"size": 1 << 20, "kind": "Flash", "bus": "USB"})[0]

        def beat():
            while not stop.wait(0.1):
                os.utime(alive)
        t = threading.Thread(target=beat)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(stop.set)
        self.gui(lambda: (time.sleep(2), self.answer([dict(id="d0", scheme="gpt", fs=fs, label="spaet")])))  # Antwort erst nach 2 s
        rc, st = self.run_job(self.disks(1 << 20), setup="alive")
        self.assertEqual((st["disks"]["d0"]["setup"], len(self.setups)), ("done", 1))

    def test_gives_up_when_the_interface_is_gone(self):
        s.ALIVE_POLLS = 5  # kein Lebenszeichen: nach etwa 2 s aufgeben
        t0 = time.time()
        rc, st = self.run_job(self.disks(1 << 20), setup="alive")
        self.assertEqual((rc, st["state"], self.setups), (0, "done", []))
        self.assertLess(time.time() - t0, 10)

    def test_cancel_while_asking(self):
        self.gui(lambda: open(os.path.join(self.tmp, "cancel"), "w").close())
        rc, st = self.run_job(self.disks(1 << 20), setup=True)
        self.assertEqual((st["state"], self.setups), ("cancelled", []))

    def test_failed_setup_is_reported_per_disk(self):
        def boom(*a):
            raise s.WipeError("kaputt")
        s.setup_disk = boom
        fs = s.setup_fs({"size": 1 << 20, "kind": "Flash", "bus": "USB"})[0]
        self.gui(lambda: self.answer([dict(id="d0", scheme="gpt", fs=fs, label="a")]))
        rc, st = self.run_job(self.disks(1 << 20), setup=True)
        x = st["disks"]["d0"]
        self.assertEqual((st["state"], x["state"], x["setup"], x["setup_error"]), ("done", "done", "error", "kaputt"))


class Formats(unittest.TestCase):
    GIB = 1024 ** 3

    def d(self, kind, size, bus="USB"):
        return dict(kind=kind, size=size, bus=bus)

    def test_recommendation_rules(self):
        stick = self.d("Flash", 16 * self.GIB)
        scheme, fs, why = s.recommend(stick)
        self.assertEqual(scheme, "mbr")
        if "fat32" in s.setup_fs(stick):
            self.assertEqual(fs, "fat32")
        self.assertEqual(s.recommend(self.d("Flash", 128 * self.GIB))[:1], ("mbr",))
        self.assertNotEqual(s.recommend(self.d("Flash", 128 * self.GIB))[1], "fat32")
        nvme = self.d("NVMe", 1000 * self.GIB, "NVMe")
        self.assertEqual(s.recommend(nvme)[:2], ("gpt", s.native_fs(nvme)))

    def test_recommendation_is_always_valid(self):
        for kind in ("Flash", "SSD", "HDD", "NVMe", "?"):
            for size in (8 * self.GIB, 40 * self.GIB, 3000 * self.GIB):
                d = self.d(kind, size)
                scheme, fs, why = s.recommend(d)
                self.assertTrue(scheme == "gpt" or size <= s.MBR_MAX)  # MBR nur bis 2 TB
                self.assertFalse(fs == "fat32" and size > s.FAT32_MAX)  # FAT32 nur bis 32 GB
                self.assertFalse(fs == "apfs" and scheme != "gpt")
                self.assertIn(fs, s.setup_fs(d))
                self.assertTrue(why)

    def test_clean_label(self):
        self.assertEqual(s.clean_label("fat32", "mein stick äöü!"), "MEIN STICK")
        self.assertEqual(s.clean_label("exfat", "Fotos 2024/Backup:XL"), "Fotos 2024B")
        self.assertEqual(s.clean_label("ntfs", "  --Daten  "), "Daten")
        self.assertEqual(s.clean_label("fat32", "###"), "NEU")
        self.assertEqual(s.clean_label("ext4", "x" * 40), "x" * 16)


class Web(Base):
    """Die Weboberfläche über die echte HTTP-Schnittstelle (ohne Browser): Zugriffsschutz, Eingabeprüfung, ganze Abläufe."""

    class Thr:  # statt eines Prozesses mit Administratorrechten läuft der Worker in einem Thread
        def __init__(self, jf):
            self.rc, self.t = None, threading.Thread(target=lambda: setattr(self, "rc", s.worker(jf)), daemon=True)
            self.t.start()

        def poll(self):
            return None if self.t.is_alive() else self.rc

        def communicate(self):
            return b"", b""

    def setUp(self):
        super().setUp()
        self.setups = []
        s.launch_worker, s.after_setup, s.SETUP_WAIT = self.Thr, lambda info: info, 20
        s.setup_disk = lambda d, scheme, fs, label, owner: (self.setups.append((d["id"], scheme, fs, label)), "Ziel-" + d["id"])[1]
        self.httpd = s.make_server()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port, self.token = self.httpd.server_address[1], self.httpd.app.token

    def call(self, method, path, body=None, **headers):
        """Antwort als (Status, Inhalt, Antwort); der Inhalt ist JSON oder Text. Eine Kopfzeile mit dem Wert None entfällt."""
        headers = {k.replace("_", "-"): v for k, v in {"X_Schredder_Token": self.token, **headers}.items() if v is not None}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body if body is None or isinstance(body, bytes) else json.dumps(body).encode(), headers)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        return r.status, (json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw.decode()), r

    def post(self, path, body=None, **headers):
        return self.call("POST", path, {} if body is None else body, **headers)

    def wait_for(self, *states, timeout=20):
        end, v = time.time() + timeout, {}
        while time.time() < end:
            v = self.call("GET", "/api/run")[1]
            if v["state"] in states:
                return v
            time.sleep(0.05)
        self.fail(f"Zustand {states} nicht erreicht, zuletzt: {v.get('state')}")

    def start(self, ds, **kw):
        s.list_disks = lambda: ds
        self.assertEqual(len(self.post("/api/scan")[1]["disks"]), len(ds))
        p = dict(ids=[d["id"] for d in ds], mode=0, verify=True, threads=2, parallel=True, confirm=ds[0]["id"] if len(ds) == 1 else str(len(ds)))
        return self.post("/api/start", dict(p, **kw))

    def test_page_and_access_control(self):
        st, body, r = self.call("GET", "/", X_Schredder_Token=None)
        self.assertEqual(st, 200)
        self.assertIn("Schredder", body)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.call("GET", "/app.js", X_Schredder_Token=None)[0], 200)
        self.assertEqual(self.call("GET", "/etc/passwd", X_Schredder_Token=None)[0], 404)
        self.assertEqual(self.call("GET", "/api/state")[0], 200)
        for headers in (dict(X_Schredder_Token=None), dict(X_Schredder_Token="falsch"), dict(Host="evil.example"),  # Token, Host (DNS-Rebinding), Herkunft
                        dict(Origin="http://evil.example")):
            self.assertEqual(self.call("GET", "/api/state", **headers)[0], 403, headers)
            self.assertEqual(self.post("/api/start", {}, **headers)[0], 403, headers)
        self.assertEqual(self.call("GET", "/api/state", Origin=f"http://127.0.0.1:{self.port}")[0], 200)

    def test_invalid_requests_start_nothing(self):
        ds = self.disks(1 << 20, 1 << 20)
        before = [self.data(d["path"]) for d in ds]
        locked = dict(ds[1], id="d9", locked="Systemplatte")
        s.list_disks = lambda: ds + [locked]
        self.post("/api/scan")
        ok = dict(ids=["d0"], mode=0, verify=True, threads=0, parallel=True, confirm="d0")
        for bad in (b"[]", b"{kaputt", dict(ok, ids=["d0", "d0"]), dict(ok, ids=["../x"]), dict(ok, ids=[]), dict(ok, ids="d0"), dict(ok, mode=7), dict(ok, mode="0"),
                    dict(ok, threads=999), dict(ok, confirm="d1"), dict(ok, confirm=""), dict(ok, ids=["d9"], confirm="d9"), dict(ok, ids=["d0", "d1"])):
            st, body, _ = self.call("POST", "/api/start", bad)
            self.assertEqual(st, 400, (bad, body))
            self.assertIn("error", body)
        self.assertEqual(self.call("GET", "/api/run")[1], dict(state="idle"))
        st, body, _ = self.post("/api/setup", {"items": []})  # keine Einrichtung angefragt
        self.assertEqual(st, 409)
        self.assertIn("nicht mehr möglich", body["error"])
        self.assertEqual(self.calls, [])
        self.assertEqual([self.data(d["path"]) for d in ds], before)
        self.assertEqual(self.post("/api/start", dict(ok, confirm="D0"))[0], 200)  # Groß-/Kleinschreibung egal
        self.wait_for("ask")
        self.post("/api/setup", {"items": []})
        self.assertEqual(self.wait_for("finished")["result"]["kind"], "ok")

    def test_full_run_with_setup_and_report(self):
        ds = self.disks(2 << 20, 3 << 20)
        self.assertEqual(self.start(ds)[0], 200)
        v = self.wait_for("ask")
        self.assertEqual([x["id"] for x in v["ask"]], ["d0", "d1"])
        self.assertTrue(all(x["rec"]["fs"] in [f["key"] for f in x["fs"]] for x in v["ask"]))
        self.assertEqual(self.post("/api/start", dict(ids=["d0"], mode=0, threads=0, confirm="d0"))[0], 409)  # läuft schon
        self.assertEqual(self.post("/api/scan")[0], 409)
        self.assertEqual(self.post("/api/back")[0], 409)
        fs = v["ask"][0]["rec"]["fs"]
        self.assertEqual(self.post("/api/setup", {"items": [dict(id="d0", scheme="gpt", fs=fs, label="Test"), dict(id="d1", scheme="gpt", fs="reiserfs", label="x")]})[0], 200)
        v = self.wait_for("finished")
        self.assertEqual(v["result"]["kind"], "warn")  # d1: ungültige Wahl, deshalb Einrichtung fehlgeschlagen
        self.assertEqual(self.setups, [("d0", "gpt", fs, s.clean_label(fs, "Test"))])
        self.assertTrue(all(self.data(d["path"]) == bytes(d["size"]) for d in ds))
        st, report, r = self.call("GET", "/api/report")
        self.assertEqual(st, 200)
        self.assertIn("text/plain", r.getheader("Content-Type"))
        for part in ("Löschprotokoll", "--- d0 ---", "--- d1 ---", "Einrichtung:", "bestanden"):
            self.assertIn(part, report)
        self.assertEqual(self.post("/api/back")[0], 200)
        self.assertEqual(self.call("GET", "/api/run")[1], dict(state="idle"))
        self.assertEqual(self.call("GET", "/api/report")[0], 404)

    def test_setup_chosen_before_start_needs_no_answer_afterwards(self):
        ds = self.disks(2 << 20, 2 << 20)
        fs = s.setup_fs(ds[0])[0]
        self.assertEqual(self.start(ds, setup=[dict(id="d0", scheme="gpt", fs=fs, label="Vorab")])[0], 200)
        v = self.wait_for("finished")  # nie „ask“: es wird nichts mehr abgefragt
        self.assertEqual(self.setups, [("d0", "gpt", fs, s.clean_label(fs, "Vorab"))])
        self.assertIn("• d1: nicht eingerichtet", v["result"]["text"])
        self.post("/api/back")
        self.assertEqual(self.start(ds, setup=[])[0], 200)  # leer = nicht einrichten
        v = self.wait_for("finished")
        self.assertEqual((len(self.setups), v["result"]["kind"]), (1, "ok"))
        self.assertIn("jetzt leer", v["result"]["text"])

    def test_invalid_setup_plan_is_refused(self):
        ds = self.disks(2 << 20)
        fs = s.setup_fs(ds[0])[0]
        for bad in ("x", [dict(id="d0", scheme="gpt", fs="reiserfs")], [dict(id="d0", scheme="xyz", fs=fs)], [dict(id="nix", scheme="gpt", fs=fs)], ["d0"],
                    [dict(id="d0", scheme="mbr", fs="apfs")]):
            self.assertEqual(self.start(ds, setup=bad)[0], 400, bad)
        self.assertEqual(self.call("GET", "/api/run")[1], dict(state="idle"))

    def test_decline_setup_and_second_run(self):
        (d,) = self.disks(1 << 20)
        for _ in range(2):  # nach „Zurück“ ist ein neuer Auftrag möglich
            self.assertEqual(self.start([d])[0], 200)
            self.wait_for("ask")
            self.post("/api/setup", {"items": []})
            v = self.wait_for("finished")
            self.assertEqual((v["result"]["kind"], self.setups), ("ok", []))
            self.assertIn("jetzt leer", v["result"]["text"])
            self.post("/api/back")

    def test_cancel_before_anything_is_written(self):
        (d,) = self.disks(2 << 20)
        before, release = self.data(d["path"]), threading.Event()

        def pf(x):
            self.calls.append(("preflight", x["id"]))
            release.wait(10)
        s.preflight = pf
        self.assertEqual(self.start([d])[0], 200)
        end = time.time() + 10
        while not self.calls and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(self.post("/api/cancel")[0], 200)
        release.set()
        v = self.wait_for("finished")
        self.assertEqual(v["result"]["kind"], "warn")
        self.assertIn("nichts verändert", v["result"]["text"])
        self.assertEqual(self.data(d["path"]), before)
        self.assertIn("ABGEBROCHEN", self.call("GET", "/api/report")[1])

    def test_without_rights_nothing_happens(self):
        ds = self.disks(1 << 20, 1 << 20)
        before = [self.data(d["path"]) for d in ds]
        s.is_admin = lambda: False
        self.assertEqual(self.start(ds)[0], 200)
        v = self.wait_for("finished")
        self.assertEqual(v["result"]["kind"], "err")
        self.assertIn("Rechte", v["result"]["text"])
        self.assertEqual(self.calls, [])
        self.assertEqual([self.data(d["path"]) for d in ds], before)

    def test_worker_dies_without_status(self):
        (d,) = self.disks(1 << 20)
        s.launch_worker = lambda jf: type("Gone", (), dict(poll=lambda self: 1, communicate=lambda self: (b"", b"")))()  # Prozess, der nie etwas schreibt und sofort beendet ist
        self.assertEqual(self.start([d])[0], 200)
        v = self.wait_for("finished")
        self.assertEqual(v["result"]["kind"], "err")
        self.assertIn("nichts verändert", v["result"]["text"])

    def test_page_text_is_escaped_by_the_client(self):
        js = self.call("GET", "/app.js", X_Schredder_Token=None)[1]
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, js)  # Namen von Datenträgern landen nur als Textknoten in der Seite


class Helpers(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(s.human(1500), "1,5 KB")
        self.assertEqual(s.human(64_023_257_088), "64,0 GB")
        self.assertEqual(s.fmt_time(3661), "1:01:01")

    def test_data_note(self):
        self.assertEqual(s.data_note({}), "Es wurde nichts verändert.")
        self.assertIn("teilweise", s.data_note({"at": "write", "done": 5}))
        self.assertIn("Prüfung", s.data_note({"at": "verify"}))
        self.assertIn("Partitionen", s.data_note({"modified": True}))


if __name__ == "__main__":
    unittest.main()
