#!/usr/bin/env python3
"""Erzeugt aus src/ je eine eigenständige schredder.py für win/, macos/ und linux/.

    python3 build.py           baut die Dateien
    python3 build.py --check   prüft nur, ob sie zu src/ passen (Exit-Code 1, falls nicht)
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PARTS = {"win": ["common", "os_win", "web"], "macos": ["common", "posix", "os_macos", "web"], "linux": ["common", "posix", "os_linux", "web"]}
ASSETS = {"UI_HTML": "ui.html", "UI_CSS": "ui.css", "UI_JS": "ui.js"}  # Weboberfläche: wird als Text in die Datei eingebettet
HEAD = "# GENERIERT von build.py aus src/ – Änderungen bitte dort machen.\n"
TAIL = '\n\nif __name__ == "__main__":\n    main()\n'


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")  # unabhängig vom Zeilenende des Checkouts


def assets():
    out = ["\n# ───────────────────────── Weboberfläche (aus src/ui.* eingebettet) ─────────────────────────"]
    for name, file in ASSETS.items():
        lines = read(os.path.join(ROOT, "src", file)).split("\n")
        out.append(f"{name} = (\n" + "\n".join("    " + repr(line + "\n") for line in lines[:-1] or lines) + "\n)")
    return "\n".join(out) + "\n"


def build(parts):
    return HEAD + "\n".join(read(os.path.join(ROOT, "src", p + ".py")).rstrip() + "\n" for p in parts) + assets() + TAIL


if __name__ == "__main__":
    check, stale = "--check" in sys.argv, []
    for target, parts in PARTS.items():
        out, text = os.path.join(ROOT, target, "schredder.py"), build(parts)
        if check:
            if not os.path.exists(out) or read(out) != text:
                stale.append(target)
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(out, 0o755)
        print("gebaut:", target)
    if stale:
        sys.exit("Veraltet: " + ", ".join(stale) + " – bitte `python3 build.py` ausführen und die Dateien mit einchecken.")
    if check:
        print("Alle schredder.py sind aktuell.")
