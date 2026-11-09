# Vorrat – Projektkontext für Claude Code

Web-App für **Haushalte (ein oder mehrere Benutzer pro Vorratsschrank)**: Vorratsverwaltung, rotierender
Speiseplan und automatische Einkaufsliste mit Zwei-Wege-Sync zu **Google Keep**.
Gehostet auf **Railway** (Docker, SQLite auf Volume). Sprache der Oberfläche: Deutsch.

## Ziel des Nutzers
- Immer wissen, was da ist und was gekauft werden muss – mit minimalem Pflegeaufwand.
- Geld sparen: im Angebot kaufen, vorkochen/einfrieren, keine Reste wegwerfen.
- Einkaufsliste landet automatisch in seiner Google-Keep-Liste (Handy im Laden).

## Stack und Entscheidungen
- **Python 3.12, Flask 3, Jinja2, sqlite3 (stdlib)** – bewusst ohne ORM und ohne JS-Build.
  Server-gerendert, einfache Formulare (POST → Redirect). Mobile-first.
- **gunicorn mit genau 1 Worker** (`--workers 1 --threads 4`): Der Keep-Sync läuft als
  Daemon-Thread im Prozess, und SQLite soll nur einen Schreibprozess haben.
  Railway-Volumes hängen an genau einem Service – deshalb kein separater Worker-Service.
- **gkeepapi** ist inoffiziell und kann brechen. Der Sync darf die App nie lahmlegen:
  Fehler werden geloggt und in `settings.keep_last_error` angezeigt.
- Login: Benutzername + Passwort (Hash in `accounts.db`), Session-Cookie (90 Tage).
  `APP_PASSWORD` ist nur noch der Nachweis, mit dem der Besitzer der bisherigen Daten beim Registrieren
  den bestehenden Schrank (Haushalt 1) übernimmt.
- **Mehrere Vorratsschränke**: pro Schrank eine eigene SQLite-Datei mit dem unveränderten Schema
  (Haushalt 1 = bisherige `vorrat.db`, weitere unter `households/<id>.db`). `accounts.db` kennt nur
  `households` (Name, Beitrittscode) und `users` (Rolle owner|member). Neue Benutzer bekommen automatisch
  einen eigenen leeren Schrank, außer sie treten per Beitrittscode einem bestehenden bei. Der Keep-Sync
  (Umgebungsvariablen = ein Konto) läuft nur für Haushalt 1.

## Struktur
```
app/__init__.py     create_app(), alle Routen
app/accounts.py     Benutzer, Schränke, Beitrittscodes (accounts.db, ohne Flask)
app/recipe_csv.py   CSV-Import von Rezepten, Vorlage und KI-Anleitung (ohne Flask)
                    Spalte `aktiv` (ja/nein) je Rezept; Import kann alle alten Rezepte vorher löschen
                    (`logic.delete_all_recipes`: behält Batch-Rezepte hinter selbstgemachten Produkten, nur nach Bestätigung
                    und nur wenn die Datei fehlerfrei ist)
app/db.py           Schema (SCHEMA), connect(), Settings-Helfer
app/logic.py        Kernlogik: Plan, Bedarf, Einkaufsliste, Bestand
app/keep_sync.py    Google-Keep-Abgleich (sync_list ist rein und testbar)
app/seed.py         lädt seed/seed.json in eine leere DB
seed/rezeptordner.json  Rezepte aus dem Rezeptordner (Fotos), Mengen wie im Buch; einmalig per seed.apply_recipe_import (überspringt `user_edited`-Rezepte)
seed/seed.json      Produkte, 35 Rezepte (pro 1 Portion, servings = 1), 5-Wochen-Rotation, Gulasch-Batch
app/templates/      Jinja-Templates (base, index, plan, einkauf, vorrat, rezepte, zutaten, login)
app/static/         style.css, manifest.json
tests/test_app.py   unittest (läuft auch mit pytest)
```

## Datenmodell (Kurzfassung)
- `products`: name, unit (g/ml/Stk/Portion/Tasse/EL/TL/Packung; im Tab „Zutaten“ änderbar, mit Umrechnung), location (Vorrat/Kühlschrank/TK), category,
  min_stock, pack_size, price, **stock**, made_by_recipe_id (selbstgemacht, z. B. Gulasch-Portion)
- `recipes` (kind `meal` | `batch`, `active` 0/1 – inaktive Gerichte sind nicht in der Rotation), `recipe_ingredients` (Menge **wie im Rezept** = eine Portion, `recipes.servings` ist immer 1; Batch: pro Batch; im Plan multipliziert `meal_plan.portions`). Rezepte aus Fotos: nur Angaben aus dem Rezept, nichts dazu interpretieren – fehlende Mengen bleiben leer, der Nutzer ergänzt sie
- `rotation(week, weekday, recipe_id)` – 5-Wochen-Rotation, Woche 1 beginnt bei `rotation_start`
- `meal_plan(date, recipe_id, portions, status planned|cooked|skipped|covered, anchor_date)` – `covered` = Reste:
  Ein Gericht kann „X Tage reichen“ (`logic.set_coverage`): Der Kochtag bekommt `portions = X`, die Folgetage werden
  `covered` (mit `anchor_date` = Kochtag), zählen nicht zum Bedarf und werden von `ensure_plan` nicht überschrieben.
  Überschreibt man einen Reste-Tag, reicht das Gericht nur bis zum Vortag; „Heute nicht“ am Kochtag gibt die Reste frei.
- `shopping_items(source auto|manual|keep, status open|bought|dismissed, keep_item_id, packs)`
- `stock_log` – jede Bestandsänderung mit Grund
- `settings` – rotation_start, rotation_weeks, keep_list_id, keep_last_sync, keep_last_error

## Kernregeln (nicht versehentlich ändern)
1. **Bedarf** = Zutaten aller *geplanten* Gerichte der nächsten `PLAN_DAYS` Tage
   + Mindestbestand − Bestand; aufgerundet auf ganze Packungen (`pack_size`).
1a. **Reserve**: haltbare Produkte (Vorrat, TK) werden für `RESERVE_DAYS` (Standard 10) Plan-Tage
   vorgehalten, Frischware (Kühlschrank) nur für `PLAN_DAYS`. Das Tiefkühlfach ist klein
   (`TK_CAPACITY_G`, Standard 4000 g; Portion ≈ 400 g): passt die TK-Reserve nicht rein, wird nur ihr Zeitraum gekürzt. Rollierend: nach dem Kochen wird nachgekauft.
2. **Selbstgemachtes** (made_by_recipe_id) landet nie auf der Liste; stattdessen werden
   die Zutaten seines Batch-Rezepts (×Anzahl nötiger Batches) eingeplant + Hinweis.
3. `ensure_plan` füllt nur fehlende Tage aus der Rotation – manuelle Änderungen bleiben.
4. `refresh_shopping_list` aktualisiert bestehende `auto`-Einträge statt sie neu anzulegen
   (Keep-Verknüpfung bleibt). Nicht mehr benötigte Einträge mit Keep-ID → `dismissed`.
   Existiert ein offener manueller Eintrag für dasselbe Produkt, wird kein auto-Eintrag erzeugt.
5. **Gekocht** zieht Zutaten × Portionen ab (Bestand nie < 0). Batch-Rezepte buchen
   zusätzlich `yields_qty` des Ergebnisprodukts ein.
6. **Gekauft** (App-Tap oder in Keep abgehakt) bucht `qty` in den Bestand.
7. Keep-Sync-Reihenfolge: (1) erledigte aus Keep löschen, (2) verknüpfte abgleichen
   (abgehakt → gekauft, gelöscht → verworfen, Text aktualisieren), (3) fremde Keep-Einträge
   importieren, (4) neue App-Einträge anlegen. Zweiter Lauf ohne Änderungen = keine Aktionen.

## Befehle
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # SESSION_COOKIE_SECURE=0 für lokal
flask --app wsgi run --debug    # oder: python wsgi.py
python -m pytest -q             # oder: python -m unittest
```
Lokale DB zurücksetzen: `rm -rf data/` (wird beim Start neu aus seed.json befüllt).

## Konventionen
- Deutsche UI-Texte, Satzanfang groß, aktive Verben auf Buttons („Gekocht“, „Neu berechnen“).
- Keine Großbuchstaben-Labels, keine Icons-Bibliothek. Farben nur über CSS-Variablen in style.css.
- Neue Logik in `logic.py` (ohne Flask-Abhängigkeit) + Test in `tests/`.
- Schemaänderungen: neue Spalten per `ALTER TABLE` in einer kleinen Migrationsfunktion in
  `db.init()` ergänzen (Produktions-DB auf Railway ist bereits befüllt – nie nur SCHEMA ändern).
- Geheimnisse nur über Umgebungsvariablen. Master-Token nie loggen.

## Roadmap (nächste Schritte, in dieser Reihenfolge)
1. **Migrationen**: einfache `schema_version` in settings + Migrationsliste in db.py.
2. **Rezepte & Produkte in der UI bearbeiten**: Rezepte (Name, Notiz, Zutaten, aktiv/inaktiv, CSV-Import unter /rezepte/import) erledigt;
   offen: Rezepte neu anlegen/löschen, Produkte bearbeiten.
3. **Backup**: nächtlicher `sqlite3 .backup`-Dump (z. B. nach S3/R2 oder per Endpoint
   `/export` als Download, geschützt durch Login). Danach Import-Funktion.
4. **Barcode-Scan** im Browser (BarcodeDetector API bzw. Fallback-Bibliothek) +
   Produktdaten von Open Food Facts (`https://world.openfoodfacts.org/api/v2/product/<ean>.json`),
   neues Feld `products.ean`.
5. **Preise**: Preis pro Packung pflegen, Wochenkosten des Plans anzeigen (Grundlage in `week_cost`).
6. **MHD** pro Bestandseintrag (eigene Tabelle `stock_batches`) mit Warnung „bald ablaufen“.
7. **Fallback Google Tasks** (offizielle API), falls gkeepapi dauerhaft bricht.
8. Optional: Rotation in der UI bearbeiten, Gerichte tauschen, „Reste“-Vorschläge.

## Bekannte Grenzen
- Neue Schränke und Schränke nach dem Rezept-Import mit „alles löschen“ haben keine Rotation: Auf der Plan-Seite
  erzeugt „Rotation aus aktiven Rezepten erzeugen“ (`logic.build_rotation`) sie zufällig verteilt neu
  (Wochen = aktive Gerichte / 7 aufgerundet, Woche 1 = diese Woche). Bearbeiten der Rotation per UI gibt es noch nicht.
- Benutzer können nicht aus einem Schrank entfernt werden (Beitrittscode neu erzeugen sperrt nur Neue aus).
- Gewürze, Salz, Pfeffer werden bewusst nicht getrackt.
- Mengen in seed.json sind Schätzwerte für 1 Portion – der Nutzer wird sie anpassen.
- Frischware ohne Mindestbestand erscheint nur, wenn ein geplantes Gericht sie braucht.
