#!/bin/bash
# Startet Schredder mit der neuesten installierten Python-3-Version (ab 3.9). Installiert Python 3 bei Bedarf (Debian/Ubuntu).
cd "$(dirname "$0")" || exit 1
newest() {  # setzt PY auf das Python mit der höchsten Versionsnummer
  local best="" bestv=0 py v
  for py in $(compgen -c | grep -E '^python3(\.[0-9]+)?$' | sort -u); do
    v=$("$py" -c 'import sys; print(sys.version_info[0] * 1000 + sys.version_info[1] if sys.version_info >= (3, 9) else 0)' 2>/dev/null) || continue
    case $v in ''|*[!0-9]*) continue;; esac
    [ "$v" -gt "$bestv" ] && best=$py bestv=$v
  done
  PY=$best
}
newest
if [ -z "$PY" ]; then
  if command -v apt-get >/dev/null; then
    echo "Python 3 fehlt – wird installiert (Administratorpasswort erforderlich) …"
    if command -v pkexec >/dev/null; then pkexec sh -c 'apt-get update && apt-get install -y python3'
    else sudo sh -c 'apt-get update && apt-get install -y python3'; fi
  else
    echo "Bitte Python 3.9 oder neuer über den Paketmanager installieren (z. B. dnf install python3)."; exit 1
  fi
  newest
  [ -n "$PY" ] || { echo "Installation fehlgeschlagen."; exit 1; }
fi
exec "$PY" schredder.py "$@"
