
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
