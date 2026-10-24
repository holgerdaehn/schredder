#!/bin/bash
# Startet Schredder mit der neuesten installierten Python-3-Version (ab 3.9). Ein Tk-Paket ist nicht nötig.
cd "$(dirname "$0")" || exit 1
best=""; bestv=0
try() {  # merkt sich das Python mit der höchsten Versionsnummer
  local v
  v=$("$1" -c 'import sys; print(sys.version_info[0] * 1000 + sys.version_info[1] if sys.version_info >= (3, 9) else 0)' 2>/dev/null) || return
  case $v in ''|*[!0-9]*) return;; esac
  [ "$v" -gt "$bestv" ] && best=$1 bestv=$v
}
for py in /Library/Frameworks/Python.framework/Versions/*/bin/python3 /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.[0-9] /opt/homebrew/bin/python3.[0-9][0-9] \
         /usr/local/bin/python3 /usr/local/bin/python3.[0-9] /usr/local/bin/python3.[0-9][0-9] "$(command -v python3)"; do
  [ -x "$py" ] && [ "$py" != /usr/bin/python3 ] && try "$py"
done
# Apples mitgeliefertes Python nur, wenn nichts Besseres da ist und die Entwicklerwerkzeuge installiert sind (sonst erscheint ein Installationsdialog)
[ -z "$best" ] && [ -x /usr/bin/python3 ] && xcode-select -p >/dev/null 2>&1 && try /usr/bin/python3
if [ -z "$best" ]; then
  echo "Kein Python 3.9 oder neuer gefunden."
  echo "Installation: https://www.python.org/downloads/macos/   oder   brew install python"
  open "https://www.python.org/downloads/macos/"
  read -n 1 -s -r -p "Beliebige Taste zum Beenden …"
  exit 1
fi
exec "$best" schredder.py "$@"
