# Vorrat

Vorratsverwaltung, 5-Wochen-Speiseplan und automatische Einkaufsliste mit
Google-Keep-Sync. Für einen Ein-Personen-Haushalt, gehostet auf Railway.

## So funktioniert es im Alltag

1. **Einmalig Inventur:** Unter *Vorrat* eintragen, was gerade da ist, und speichern.
2. **Vor dem Einkauf:** Unter *Einkauf* auf „Neu berechnen“ tippen. Die App nimmt die
   Gerichte der nächsten 7 Tage, zieht deinen Vorrat ab, ergänzt Mindestbestände und
   rundet auf Packungen. Haltbare Zutaten (Vorrat, TK) hältst du zusätzlich für
   10 Tage Speiseplan vorrätig (`RESERVE_DAYS`), Frischware nur für 7. Läuft der Keep-Sync, landet alles automatisch in Keep.
3. **Im Laden:** In Keep (oder in der App) abhaken. Abgehaktes wird als gekauft in den
   Vorrat gebucht.
4. **Nach dem Kochen:** Auf *Heute* „Gekocht“ tippen. Die Zutaten werden abgezogen.
5. **Gulasch:** Rezept „Gulasch kochen (Batch, 4 Portionen)“ unter *Rezepte* als gekocht
   buchen → 4 Gulasch-Portionen liegen im TK und decken die Gulasch-Samstage ab.

## Lokal starten

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # APP_PASSWORD setzen, SESSION_COOKIE_SECURE=0
set -a; source .env; set +a
python wsgi.py         # http://localhost:5000
python -m pytest -q
```

## Auf Railway deployen

1. Repo auf GitHub pushen, in Railway **New Project → Deploy from GitHub repo**.
   Das `Dockerfile` und `railway.toml` werden automatisch verwendet.
2. Im Service **Volume hinzufügen**, Mount-Pfad **`/data`**.
3. Unter *Variables* setzen:
   - `APP_PASSWORD` – langes Passwort
   - `SECRET_KEY` – `python -c "import secrets; print(secrets.token_hex(32))"`
   - optional `ROTATION_START` (ein Montag, der als Rotationswoche 1 zählt)
   - optional die `KEEP_*`-Variablen (siehe unten)
4. Unter *Settings → Networking* eine Domain generieren. Auf dem Handy öffnen und
   in Chrome (Android) das Menü ⋮ → *App installieren* (bzw. *Zum Startbildschirm hinzufügen*).

`DATA_DIR=/data` ist im Dockerfile gesetzt. Beim ersten Start wird die Datenbank aus
`seed/seed.json` befüllt; danach wird seed.json nicht mehr gelesen.

## Google Keep einrichten (optional)

Google bietet für private Konten keine offizielle Keep-API. Die App nutzt deshalb
[gkeepapi](https://github.com/kiwiz/gkeepapi) (inoffiziell). Das kann jederzeit brechen;
die App läuft dann ohne Sync weiter und zeigt den Fehler auf der Einkaufsseite.

**Empfohlen:** ein eigenes Google-Konto nur für die App anlegen. Der Master-Token gibt
vollen Zugriff auf das Konto – bei einem Wegwerf-Konto ist das unkritisch.

1. Mit dem App-Konto in Keep eine Liste „Einkaufsliste“ anlegen (oder die App legt sie an)
   und sie **mit deinem Hauptkonto teilen**.
2. Master-Token erzeugen – siehe Abschnitt „Obtaining a Master Token“ in der
   gkeepapi-Dokumentation (über gpsoauth). Die Methoden ändern sich gelegentlich;
   in den gkeepapi- und Home-Assistant-Foren gibt es jeweils aktuelle Anleitungen.
3. In Railway setzen: `KEEP_EMAIL`, `KEEP_MASTER_TOKEN`, optional `KEEP_LIST_TITLE`
   und `KEEP_SYNC_INTERVAL_MIN` (Standard 10).

Der Sync läuft alle X Minuten im Hintergrund und kann auf der Einkaufsseite manuell
angestoßen werden.

## Kosten

Railway Hobby-Plan: 5 $/Monat inkl. 5 $ Nutzung. Eine App dieser Größe (1 kleiner
Container + wenige MB Volume) sollte im inkludierten Kontingent bleiben.

## Weiterentwickeln

Siehe `CLAUDE.md` – dort stehen Architektur, Kernregeln und die Roadmap für Claude Code.
