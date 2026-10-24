# Änderungsprotokoll

Alle nennenswerten Änderungen an diesem Projekt. Das Format folgt
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/), die Versionierung [SemVer](https://semver.org/lang/de/).

## [1.0.0] – 2026-10-24

Erste Veröffentlichung.

### Hinzugefügt
- Komplette Datenträger sicher löschen für Windows 10/11, macOS und Linux (Ubuntu). Die Oberfläche ist eine lokale Webseite
  (eigenes App-Fenster in Edge/Chrome/Brave/Chromium, sonst Standardbrowser) – nur Python-Standardbibliothek, kein Tk, keine
  weiteren Pakete.
- Läuft mit jeder Python-3-Version ab 3.9; die Startdateien wählen immer die neueste installierte und installieren bei Bedarf
  die neueste (Windows: `winget`, Linux: `apt`).
- Methoden *Schnell* (1× Nullen), *Sicher* (1× Zufall + Nullen) und *Gründlich* (3× Zufall + Nullen); optionale Prüfung durch
  Zurücklesen.
- Nach dem Löschen das Angebot, den Datenträger als neues Laufwerk einzurichten: Partitionstabelle (GPT/MBR) und Dateisystem
  (NTFS, exFAT, FAT32, APFS, Mac OS Extended, ext4) werden nach Art, Größe und Betriebssystem automatisch empfohlen und lassen
  sich ändern; dieselbe Rechte-Abfrage genügt. Unter Windows wird geprüft, dass das neue Laufwerk Buchstaben und Dateisystem hat.
- Mehrere Datenträger gleichzeitig oder nacheinander mit einer einzigen Rechte-Abfrage; Fortschritt, Geschwindigkeit und
  Restzeit je Datenträger und gesamt; Löschprotokoll als Textdatei.
- Sicherheit: ohne Administrator-/Root-Rechte wird nichts verändert; nicht zerstörende Vorprüfung vor jeder Änderung;
  Systemplatte, interne Mac-Laufwerke und der Datenträger mit dem Programm sind gesperrt; der privilegierte Worker prüft die
  Auswahl erneut; Bestätigung durch Eintippen von Name bzw. Anzahl (auch auf dem Server geprüft); klare Meldung, falls nach dem
  Entfernen der Partitionen etwas scheitert.
- Schutz der lokalen Oberfläche: nur `127.0.0.1`, zufälliges Token je Start, Prüfung von Host und Herkunft, strikte
  Content-Security-Policy; das Programm beendet sich, wenn das Fenster geschlossen wird – nie mitten in einem Löschvorgang.
- Leistung: ungepufferte Ein-/Ausgabe (`O_DIRECT`, `FILE_FLAG_NO_BUFFERING`, Rohgerät), 4-MiB-Blöcke, parallele Threads je
  Medium, schneller Zufallspool.
- Entwicklung: gemeinsamer Quellcode in `src/` mit `build.py` (erzeugt je System eine eigenständige Datei), Tests (Engine,
  Worker, HTTP-Schnittstelle) und CI mit Python 3.9 und der jeweils neuesten 3.x unter Linux, Windows und macOS.

[1.0.0]: https://github.com/holgerdaehn/schredder/releases/tag/v1.0.0
