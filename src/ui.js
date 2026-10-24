"use strict";
const $ = (id) => document.getElementById(id);
const TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || "";
const TITLE = "Schredder – Datenträger sicher löschen";
const S = { info: null, disks: [], sel: new Set(), peek: null, mode: 0, view: null, scanning: false, looping: false, err: "", offline: false };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Element bauen – Texte immer als Textknoten (Namen von Datenträgern dürfen nie als HTML gedeutet werden)
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}

// Hinweis (nur ein Knopf) oder Rückfrage (zwei Knöpfe) als Dialog der Seite – alert/confirm sperren manche Browser
function msg(title, text, yes, no) {
  return new Promise((resolve) => {
    const d = $("d-msg");
    d.returnValue = "";
    d.addEventListener("close", () => resolve(d.returnValue === "yes"), { once: true });
    d.replaceChildren(h("div", { class: "dlg" }, h("h3", {}, title), h("p", {}, text),
      h("div", { class: "btns" }, no && h("button", { class: "btn", type: "button", onclick: () => d.close("no") }, no),
        h("button", { class: "btn primary", type: "button", onclick: () => d.close("yes") }, yes))));
    d.showModal();
  });
}

async function api(path, body) {
  const r = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Schredder-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* leere Antwort */ }
  if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
  return data;
}

function human(n, dec = 1) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < 4) { n /= 1000; i++; }
  return (i ? n.toFixed(dec) : String(Math.round(n))).replace(".", ",") + " " + u[i];
}
const typ = (d) => (!d.bus || d.bus.toLowerCase() === d.kind.toLowerCase() ? d.kind : `${d.bus} · ${d.kind}`);
const selected = () => S.disks.filter((d) => S.sel.has(d.id));
const active = () => S.view && ["launching", "running", "ask", "setup"].includes(S.view.state);

// ───── Datenträgerliste ─────
function renderDisks() {
  const tb = $("disks").tBodies[0];
  tb.replaceChildren();
  if (!S.disks.length) {
    tb.append(h("tr", { class: "empty" }, h("td", { colspan: 6 }, S.scanning ? "Suche Datenträger …" : "Keine Datenträger gefunden. Datenträger anschließen und „Aktualisieren“ klicken.")));
  }
  for (const d of S.disks) {
    const cb = h("input", { type: "checkbox", disabled: !!d.locked, "aria-label": d.id });
    cb.checked = S.sel.has(d.id);
    const tr = h("tr", { class: (d.locked ? "locked " : "") + (cb.checked ? "sel" : ""), title: d.locked ? "Gesperrt: " + d.locked : "" },
      h("td", {}, cb), h("td", {}, d.id), h("td", {}, d.model || "–"), h("td", { class: "r" }, d.size_h), h("td", {}, typ(d)),
      h("td", {}, d.locked ? "Gesperrt: " + d.locked : d.volumes.join(", ") || "–"));
    tr.addEventListener("click", (e) => {
      if (d.locked) { S.peek = d; return updateSel(); }
      if (e.target !== cb) cb.checked = !cb.checked;
      if (cb.checked) S.sel.add(d.id); else S.sel.delete(d.id);
      S.peek = null;
      tr.classList.toggle("sel", cb.checked);
      updateSel();
    });
    tb.append(tr);
  }
  const n = S.disks.filter((d) => !d.locked).length;
  $("scan-info").textContent = S.scanning ? "Suche Datenträger …" : `${n} löschbar` + (S.disks.length > n ? `, ${S.disks.length - n} gesperrt` : "");
}

function updateSel() {
  const sel = selected();
  let txt = S.err;
  if (!txt && sel.length === 1) {
    const d = sel[0];
    txt = `${d.model || "Unbekanntes Modell"}${d.serial ? "  ·  Seriennr. " + d.serial : ""}\n${d.size_h} (${d.size.toLocaleString("de-DE")} Bytes)  ·  ${d.bus || "?"}  ·  ${d.kind}  ·  Gerät ${d.path}`;
    if (["SSD", "NVMe", "Flash"].includes(d.kind)) txt += "\nHinweis: Flash-Speicher kann Restdaten behalten – für höchste Sicherheit zusätzlich Secure Erase des Herstellers nutzen.";
  } else if (!txt && sel.length > 1) {
    const names = sel.map((d) => d.id).join(", ");
    txt = `${sel.length} Datenträger ausgewählt – zusammen ${human(sel.reduce((a, d) => a + d.size, 0))}\n${names.length < 100 ? names : names.slice(0, 97) + "…"}`;
  } else if (!txt && S.peek) {
    txt = `${S.peek.model || "Unbekanntes Modell"}  ·  ${S.peek.size_h}\nGesperrt: ${S.peek.locked}`;
  }
  $("detail").textContent = txt;
  const d = $("detail");
  d.style.color = S.err || (!sel.length && S.peek) ? "var(--red)" : "";
  $("b-go").disabled = !sel.length || active() || S.offline;
  $("b-go").textContent = sel.length > 1 ? `${sel.length} Datenträger löschen …` : "Datenträger löschen …";
  $("o-flow").disabled = sel.length < 2;
  $("hint").textContent = sel.length ? (S.info && S.info.admin ? "" : "Beim Löschen werden Administratorrechte angefordert.") : "Einen oder mehrere Datenträger anhaken.";
}

async function scan() {
  S.scanning = true; S.err = ""; S.sel.clear(); S.peek = null;
  $("b-refresh").disabled = true;
  renderDisks(); updateSel();
  try { S.disks = (await api("scan", {})).disks; }
  catch (e) { S.disks = []; S.err = "Datenträger konnten nicht gelesen werden: " + e.message; }
  S.scanning = false;
  $("b-refresh").disabled = false;
  renderDisks(); updateSel();
}

function renderModes() {
  const box = $("modes");
  box.replaceChildren();
  S.info.modes.forEach((m, i) => box.append(h("button", { type: "button", class: "mode" + (i === S.mode ? " on" : ""), role: "radio", "aria-checked": String(i === S.mode), onclick: () => { S.mode = i; renderModes(); } },
    h("b", {}, m.name), h("span", { class: "sub" }, m.sub), h("span", { class: "info" }, m.info))));
}

// ───── Bestätigung und Start ─────
function confirmDialog(plan) {
  const sel = selected(), n = sel.length, word = n === 1 ? sel[0].id : String(n), mode = S.info.modes[S.mode];
  const planText = (x) => {
    const it = plan.find((p) => p.id === x.id), f = it && x.setup.fs.find((q) => q.key === it.fs);
    return it ? `Danach: als neues Laufwerk einrichten – ${it.scheme.toUpperCase()}, ${f ? f.name : it.fs}, Name „${it.label}“` : "Danach: nicht einrichten (der Datenträger bleibt leer und im System unsichtbar)";
  };
  const verify = $("o-verify").checked, par = n > 1 && $("o-flow").value === "true", d = $("d-confirm");
  const input = h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Bestätigung" });
  const ok = h("button", { class: "btn danger", type: "button", disabled: true }, "Endgültig löschen");
  input.addEventListener("input", () => { ok.disabled = input.value.trim().toLowerCase() !== word.toLowerCase(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !ok.disabled) ok.click(); });
  ok.addEventListener("click", async () => {
    ok.disabled = true;
    try {
      await api("start", { ids: sel.map((x) => x.id), mode: S.mode, verify, threads: Number($("o-perf").value), parallel: par, confirm: input.value, setup: plan });
      d.close();
      S.view = { state: "launching" };
      showRun();
    } catch (e) { msg("Start nicht möglich", e.message, "OK"); ok.disabled = false; }
  });
  d.replaceChildren(h("div", { class: "dlg" },
    h("h3", { class: "red" }, n === 1 ? "Datenträger endgültig löschen?" : `${n} Datenträger endgültig löschen?`),
    h("div", { class: "box" }, sel.flatMap((x) => [h("b", {}, `${x.id}  ·  ${x.model || "Unbekanntes Modell"}  ·  ${x.size_h}`), h("small", {}, "Inhalt: " + (x.volumes.join(", ") || "keine erkennbaren Laufwerke")), h("small", {}, planText(x))]),
      h("small", {}, `Methode: ${mode.name} (${mode.passes.join(" → ")})` + (verify ? "  +  Prüfung" : "") + (n > 1 ? `  ·  ${par ? "gleichzeitig" : "nacheinander"}` : ""))),
    h("p", {}, (n === 1 ? "Alle Daten gehen unwiderruflich verloren. " : `Alle Daten auf diesen ${n} Datenträgern gehen unwiderruflich verloren. `)
      + (n === 1 ? `Zur Bestätigung „${word}“ eingeben:` : `Zur Bestätigung die Anzahl „${word}“ eingeben:`)),
    input,
    h("div", { class: "btns" }, h("button", { class: "btn", type: "button", onclick: () => d.close() }, "Abbrechen"), ok)));
  d.showModal();
  input.focus();
}

// ───── Fortschritt ─────
function showSelect() {
  $("v-run").hidden = true; $("v-select").hidden = false; $("bar").hidden = false; document.title = TITLE;
}

function showRun() {
  $("v-select").hidden = true; $("v-run").hidden = false; $("bar").hidden = true;
  pollLoop();
}

function drawChart(hist) {
  const c = $("r-chart"), dpr = window.devicePixelRatio || 1, w = c.clientWidth, hh = c.clientHeight;
  if (!w || !hh) return;
  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(hh * dpr); }
  const x = c.getContext("2d"), st = getComputedStyle(document.documentElement);
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, w, hh);
  if (hist.length < 2) return;
  const top = (Math.max(...hist) * 1.15) || 1, pts = hist.map((v, i) => [(i / (hist.length - 1)) * w, hh - 2 - (v / top) * (hh - 8)]);
  x.beginPath(); x.moveTo(0, hh); pts.forEach((p) => x.lineTo(p[0], p[1])); x.lineTo(w, hh); x.closePath();
  x.fillStyle = st.getPropertyValue("--chart"); x.fill();
  x.beginPath(); pts.forEach((p, i) => (i ? x.lineTo(p[0], p[1]) : x.moveTo(p[0], p[1])));
  x.strokeStyle = st.getPropertyValue("--acc"); x.lineWidth = 2; x.stroke();
  x.fillStyle = st.getPropertyValue("--mute"); x.font = "12px sans-serif";
  x.fillText("max. " + human(top / 1.15, 0) + "/s", 8, 16);
}

function renderRun(v) {
  $("r-title").textContent = v.title || "";
  $("r-phase").textContent = v.phase || "";
  $("r-pct").textContent = v.pct || ""; $("r-pct").dataset.c = v.col || "acc";
  $("r-meter").dataset.c = v.col || "acc"; $("r-meter").firstElementChild.style.setProperty("--v", v.frac || 0);
  for (const k of ["speed", "avg", "el", "eta", "done"]) $("s-" + k).textContent = (v.stats && v.stats[k]) || "–";
  const rows = $("r-rows"), top = rows.scrollTop;
  rows.hidden = !v.multi;
  if (v.multi) {
    rows.replaceChildren(...v.rows.map((r) => {
      const bar = h("div", { class: "bar-s", "data-c": r.col }, h("i"));
      bar.firstElementChild.style.setProperty("--v", r.frac);
      return h("div", { class: "rowd", "data-c": r.col }, h("div", { class: "nm" }, r.id, h("span", {}, r.sub)), h("div", { class: "pc" }, r.pct), bar,
        h("div", { class: "ph" }, r.phase), h("div", { class: "rt" }, r.rate));
    }));
    rows.scrollTop = top;
  }
  const fin = v.state === "finished";
  $("b-cancel").hidden = fin; $("b-cancel").disabled = !v.can_cancel;
  $("r-result").hidden = !fin;
  if (fin) { $("r-banner").className = "banner " + v.result.kind; $("r-banner").textContent = v.result.text; }
  $("r-note").hidden = !v.note; $("r-note").textContent = v.note || "";
  drawChart(v.hist || []);
  document.title = fin || !v.frac ? TITLE : `Schredder – ${Math.round(v.frac * 100)} %`;
}

async function pollLoop() {
  if (S.looping) return;
  S.looping = true;
  try {
    while (S.view && S.view.state !== "idle") {
      try { S.view = await api("run"); } catch (e) { offline(); break; }
      renderRun(S.view);
      if (S.view.state === "ask") await askSetup(S.view.ask);
      if (S.view.state === "finished") break;
      await sleep(500);
    }
  } finally { S.looping = false; }
}

// ───── Einrichtung als Laufwerk ─────
// Dialog mit Format-Vorschlag je Datenträger; liefert die Wahl (Liste) oder o.decline / o.esc
function pickSetup(items, o) {
  return new Promise((resolve) => {
    const d = $("d-setup");
    let done = false;
    const rows = items.map((it) => {
      const on = h("input", { type: "checkbox" }), fs = h("select"), sch = h("select"), label = h("input", { type: "text", size: "14", maxlength: "32", "aria-label": "Name" });
      const why = h("div", { class: "why" });
      on.checked = true; label.value = it.label;
      fs.append(...it.fs.map((f) => h("option", { value: f.key }, f.name)));
      fs.value = it.rec.fs;
      let cur = it.rec.scheme;
      const refresh = () => {
        const opts = it.schemes[fs.value];
        sch.replaceChildren(...opts.map((s) => h("option", { value: s }, s.toUpperCase())));
        sch.value = opts.includes(cur) ? cur : opts[0];
        sch.disabled = opts.length < 2;
        cur = sch.value;
        why.textContent = (sch.value === it.rec.scheme && fs.value === it.rec.fs ? "Empfohlen: " : "Gewählt: ") + it.why[`${sch.value}|${fs.value}`];
      };
      fs.addEventListener("change", refresh); sch.addEventListener("change", () => { cur = sch.value; refresh(); });
      refresh();
      return { id: it.id, on, fs, sch, label,
        el: h("div", { class: "sdisk" }, h("label", {}, on, `${it.id}  ·  ${it.model}  ·  ${it.size_h}`),
          h("div", { class: "ctl" }, h("label", {}, "Partitionstabelle", sch), h("label", {}, "Dateisystem", fs), h("label", {}, "Name", label)), why) };
    });
    const finish = (v) => {
      if (done) return;
      done = true; d.close(); resolve(v);
    };
    d.addEventListener("close", () => finish(o.esc), { once: true }); // Esc oder Schließen
    d.replaceChildren(h("div", { class: "dlg" }, h("h3", {}, o.title), h("p", { class: "mute" }, o.intro), rows.map((r) => r.el),
      h("div", { class: "btns" }, h("button", { class: "btn", type: "button", onclick: () => finish(o.decline) }, o.no),
        h("button", { class: "btn primary", type: "button", onclick: () => finish(rows.filter((r) => r.on.checked).map((r) => ({ id: r.id, scheme: r.sch.value, fs: r.fs.value, label: r.label.value }))) }, o.ok))));
    d.showModal();
  });
}

// Rückfrage nach dem Löschen – nur noch, wenn die Einrichtung nicht schon vor dem Start festgelegt wurde
async function askSetup(items) {
  const list = await pickSetup(items, { title: "Als neues Laufwerk einrichten?", ok: "Einrichten", no: "Nicht einrichten", decline: [], esc: [],
    intro: "Gelöschte Datenträger sind leer und im System nicht mehr als Laufwerk sichtbar. Schredder kann sie jetzt neu einrichten (Partitionstabelle und Dateisystem). Das passende Format wurde nach Art und Größe des Datenträgers und nach Betriebssystem erkannt; es lässt sich ändern." });
  try { await api("setup", { items: list }); } catch (e) { msg("Einrichtung nicht möglich", e.message, "OK"); }
}

// Einrichtung vor dem Start festlegen: null = Vorgang abbrechen
async function planSetup(sel) {
  const how = $("o-setup").value;
  if (how === "none") return [];
  if (how === "auto") return sel.map((d) => ({ id: d.id, scheme: d.setup.rec.scheme, fs: d.setup.rec.fs, label: d.setup.label }));
  return pickSetup(sel.map((d) => d.setup), { title: "Format für die neuen Laufwerke wählen", ok: "Weiter", no: "Abbrechen", decline: null, esc: null,
    intro: "Nach dem Löschen richtet Schredder die Datenträger so ein. Die Vorschläge richten sich nach Art, Größe und Betriebssystem; abgewählte Datenträger bleiben leer." });
}

// ───── Protokoll, Verbindung, Start ─────
async function saveReport() {
  try {
    const r = await fetch("/api/report", { headers: { "X-Schredder-Token": TOKEN } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const t = await r.text(), n = new Date(), p = (x) => String(x).padStart(2, "0");
    const a = h("a", { href: URL.createObjectURL(new Blob([t], { type: "text/plain;charset=utf-8" })), download: `Schredder-Protokoll-${n.getFullYear()}${p(n.getMonth() + 1)}${p(n.getDate())}-${p(n.getHours())}${p(n.getMinutes())}.txt` });
    document.body.append(a); a.click(); a.remove();
  } catch (e) { msg("Protokoll", "Das Protokoll konnte nicht gespeichert werden: " + e.message, "OK"); }
}

function offline() {
  S.offline = true; $("offline").hidden = false; updateSel();
}

async function init() {
  if (!TOKEN) { $("offline").textContent = "Bitte Schredder über die Startdatei (Schredder.bat, Schredder.command oder Schredder.sh) starten."; $("offline").hidden = false; return; }
  try { S.info = await api("state"); } catch (e) { return offline(); }
  const i = S.info;
  $("ver").textContent = `Version ${i.version}`;
  $("rights").textContent = i.admin ? "Administratorrechte: vorhanden" : "Administratorrechte: werden beim Löschen angefordert";
  $("about").textContent = `Schredder ${i.version} · Python ${i.python} · ${i.system}`;
  $("o-flow").append(...i.flow.map((f) => h("option", { value: String(f.value) }, f.name)));
  $("o-perf").append(...i.perf.map((f) => h("option", { value: String(f.value) }, f.name)));
  renderModes();
  $("b-all").addEventListener("click", () => { S.sel = new Set(S.disks.filter((d) => !d.locked).map((d) => d.id)); S.peek = null; renderDisks(); updateSel(); });
  $("b-refresh").addEventListener("click", scan);
  $("b-go").addEventListener("click", async () => {
    const plan = await planSetup(selected());
    if (plan) confirmDialog(plan);
  });
  $("b-cancel").addEventListener("click", async () => {
    if (!(await msg("Löschvorgang abbrechen?", "Bereits begonnene Datenträger bleiben teilweise überschrieben.", "Ja, abbrechen", "Weiter löschen"))) return;
    try { await api("cancel", {}); } catch (e) { msg("Abbrechen nicht möglich", e.message, "OK"); }
  });
  $("b-report").addEventListener("click", saveReport);
  $("b-back").addEventListener("click", async () => {
    try { await api("back", {}); } catch (e) { return msg("Zurück nicht möglich", e.message, "OK"); }
    S.view = null; showSelect(); scan();
  });
  setInterval(() => api("ping", {}).catch(offline), 3000);
  addEventListener("pagehide", () => { fetch("/api/bye", { method: "POST", keepalive: true, headers: { "X-Schredder-Token": TOKEN }, body: "{}" }).catch(() => {}); });
  addEventListener("beforeunload", (e) => { if (active()) { e.preventDefault(); e.returnValue = ""; } });
  if (i.run) { S.view = i.run; showRun(); } else { showSelect(); scan(); }
  updateSel();
}
init();
