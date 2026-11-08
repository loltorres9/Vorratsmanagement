"""Rezept-Import per CSV (ohne Flask-Abhängigkeit).

Eine Zeile = eine Zutat eines Rezepts. Zeilen mit gleichem `rezept` gehören zusammen.
Mengen gelten für EINE Portion. Die Vorlage und die KI-Anleitung entstehen aus denselben
Konstanten wie der Parser und bleiben so mit ihm im Einklang.
"""
import csv
import io

from . import logic

COLUMNS = ["rezept", "notiz", "zutat", "menge", "einheit", "lagerort", "kategorie", "packungsgroesse", "aktiv"]
REQUIRED = ("rezept", "zutat", "menge")

ALIASES = {
    "rezept": "rezept", "gericht": "rezept", "name": "rezept",
    "notiz": "notiz", "notizen": "notiz", "zubereitung": "notiz", "hinweis": "notiz",
    "zutat": "zutat", "zutaten": "zutat",
    "menge": "menge",
    "einheit": "einheit",
    "lagerort": "lagerort", "lager": "lagerort",
    "kategorie": "kategorie",
    "packungsgroesse": "packungsgroesse", "packungsgröße": "packungsgroesse", "packung": "packungsgroesse",
    "aktiv": "aktiv", "active": "aktiv",
}
YES = {"ja", "j", "1", "true", "yes", "aktiv", "x"}
NO = {"nein", "n", "0", "false", "no", "inaktiv"}
UNIT_ALIASES = {"stück": "Stk", "stueck": "Stk", "st": "Stk", "st.": "Stk", "stk.": "Stk",
                "pck": "Packung", "pkg": "Packung", "päckchen": "Packung", "packung(en)": "Packung",
                "el": "EL", "tl": "TL", "portionen": "Portion", "tassen": "Tasse"}
UNIT_FACTORS = {"kg": ("g", 1000), "l": ("ml", 1000), "liter": ("ml", 1000), "cl": ("ml", 10), "dl": ("ml", 100)}
LOCATION_ALIASES = {"kuehlschrank": "Kühlschrank", "kühl": "Kühlschrank", "kuehl": "Kühlschrank",
                    "tiefkühl": "TK", "tiefkuehl": "TK", "tiefkühlfach": "TK", "tiefkühler": "TK",
                    "gefrierfach": "TK", "vorratsschrank": "Vorrat", "schrank": "Vorrat"}

EXAMPLE = [
    ["Spaghetti Bolognese", "Hack anbraten, Soße 20 Minuten köcheln lassen.", "Spaghetti", "125", "g", "Vorrat", "Nudeln", "500", "ja"],
    ["Spaghetti Bolognese", "", "Rinderhack", "150", "g", "Kühlschrank", "Fleisch", "500", ""],
    ["Spaghetti Bolognese", "", "Passierte Tomaten", "200", "g", "Vorrat", "Konserven", "500", ""],
    ["Spaghetti Bolognese", "", "Zwiebel", "1", "Stk", "Vorrat", "Obst & Gemüse", "", ""],
    ["Gemüsepfanne mit Reis", "", "Reis", "80", "g", "Vorrat", "Trockenwaren", "1000", "nein"],
    ["Gemüsepfanne mit Reis", "", "Paprika", "1", "Stk", "Kühlschrank", "Obst & Gemüse", "", ""],
]


class CsvError(ValueError):
    """Datei nicht lesbar (kein CSV, Pflichtspalte fehlt …)."""


# ----------------------------------------------------------------- Vorlage & Anleitung

def template_csv() -> str:
    """Vorlage mit Beispielrezepten; mit BOM, damit Excel die Umlaute richtig anzeigt."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(COLUMNS)
    w.writerows(EXAMPLE)
    return "﻿" + out.getvalue()


def ai_instructions() -> str:
    units = ", ".join(logic.UNITS)
    places = ", ".join(logic.LOCATIONS)
    return f"""Du erzeugst eine CSV-Datei mit Rezepten für meine Vorrats-App.

FORMAT
- Reiner CSV-Text, Trennzeichen Semikolon (;), UTF-8, erste Zeile ist die Kopfzeile.
- Spalten in genau dieser Reihenfolge: {";".join(COLUMNS)}
- Eine Zeile pro Zutat. Alle Zeilen eines Rezepts tragen denselben Rezeptnamen.
- Gib NUR die CSV aus (in einem Codeblock), keinen Begleittext. Felder mit Semikolon oder Zeilenumbruch in "Anführungszeichen".

SPALTEN
- rezept (Pflicht): Name des Gerichts, eindeutig.
- notiz (optional): Zubereitung oder Hinweis, kurz. Nur in der ersten Zeile des Rezepts nötig.
- zutat (Pflicht): Name der Zutat, einheitlich geschrieben (immer "Rinderhack", nie mal "Hackfleisch"). Gleiche Zutat in mehreren Rezepten = exakt gleicher Name.
- menge (Pflicht): Zahl > 0 für EINE Portion. Dezimalpunkt oder -komma, keine Brüche wie 1/2, keine Einheit im Feld.
- einheit (Pflicht): genau eine von: {units}. (kg und l rechnet die App in g und ml um.)
- lagerort (nur bei neuen Zutaten): {places}. Leer = Vorrat. Frisches (Fleisch, Gemüse, Milch) = Kühlschrank, Tiefgefrorenes = TK.
- kategorie (optional): Abteilung im Supermarkt, z. B. Nudeln, Fleisch, Molkerei, Obst & Gemüse, Konserven, Backen, Tiefkühl.
- packungsgroesse (optional): übliche Packungsgröße in derselben Einheit wie menge (z. B. 500 für 500 g). Daraus rundet die App beim Einkauf auf ganze Packungen.
- aktiv (optional, pro Rezept): ja = kommt in die Rotation, nein = wird nur angelegt. Leer = ja. Nur in der ersten Zeile des Rezepts nötig.

REGELN
- Salz, Pfeffer und Gewürze weglassen (die App verfolgt sie nicht).
- Mengen realistisch für eine Person und eine Portion.
- Keine leeren Zeilen, keine Kommentare, keine zusätzlichen Spalten.

BEISPIEL
{template_csv().lstrip(chr(0xFEFF)).strip()}

MEIN WUNSCH
(Hier eintragen, z. B. "10 schnelle vegetarische Abendessen unter 30 Minuten, günstig, gut vorzukochen".)
"""


# ------------------------------------------------------------------------ Parsen

def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _delimiter(text: str) -> str:
    head = text.splitlines()[0] if text.strip() else ""
    return max(";\t,", key=lambda c: head.count(c)) if any(c in head for c in ";\t,") else ";"


def _number(text):
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def _unit(text):
    """(Einheit, Faktor auf die Einheit der App) oder None bei Unbekanntem; '' bei leerer Angabe."""
    t = text.strip()
    if not t:
        return "", 1
    low = t.lower()
    for u in logic.UNITS:
        if low == u.lower():
            return u, 1
    if low in UNIT_ALIASES:
        return UNIT_ALIASES[low], 1
    if low in UNIT_FACTORS:
        return UNIT_FACTORS[low]
    return None


def _location(text):
    t = text.strip()
    if not t:
        return ""
    low = t.lower()
    for loc in logic.LOCATIONS:
        if low == loc.lower():
            return loc
    return LOCATION_ALIASES.get(low)


def parse(text: str):
    """Liest die CSV. Liefert (Rezepte, Fehler).

    Ein Rezept mit fehlerhafter Zeile wird als Ganzes ausgelassen und der Fehler gemeldet; die
    übrigen Rezepte bleiben gültig. Wirft CsvError, wenn die Datei als Ganzes unbrauchbar ist.
    """
    if not text.strip():
        raise CsvError("Die Datei ist leer.")
    rows = list(csv.reader(io.StringIO(text), delimiter=_delimiter(text)))
    header = [ALIASES.get(h.strip().lower()) for h in rows[0]]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise CsvError("In der Kopfzeile fehlen die Spalten: " + ", ".join(missing)
                           + ". Erwartet: " + ";".join(COLUMNS))
    recipes, errors, bad = {}, [], set()

    for n, row in enumerate(rows[1:], start=2):
        cells = {}
        for col, val in zip(header, row):
            if col and col not in cells:
                cells[col] = val.strip()
        if not any(cells.values()) or (row and row[0].lstrip().startswith("#")):
            continue
        rname = cells.get("rezept", "")
        if not rname:
            errors.append(f"Zeile {n}: Der Rezeptname fehlt.")
            continue
        rec = recipes.setdefault(rname.casefold(), {"name": rname, "notes": "", "ingredients": {}, "active": None})
        if cells.get("notiz") and not rec["notes"]:
            rec["notes"] = cells["notiz"]

        def fail(msg):
            errors.append(f"Zeile {n} ({rname}): {msg}")
            bad.add(rname.casefold())

        flag = cells.get("aktiv", "").lower()
        if flag and rec["active"] is None:
            if flag in YES or flag in NO:
                rec["active"] = flag in YES
            else:
                fail(f"„aktiv“ muss ja oder nein sein, nicht „{cells['aktiv']}“.")

        zutat = cells.get("zutat", "")
        qty = _number(cells.get("menge", ""))
        unit = _unit(cells.get("einheit", ""))
        loc = _location(cells.get("lagerort", ""))
        pack = _number(cells["packungsgroesse"]) if cells.get("packungsgroesse") else None
        if not zutat:
            fail("Die Zutat fehlt.")
        elif qty is None or qty <= 0:
            fail(f"Die Menge von „{zutat}“ ist keine Zahl größer als 0.")
        elif unit is None:
            fail(f"Die Einheit „{cells.get('einheit')}“ ist unbekannt. Erlaubt: {', '.join(logic.UNITS)}.")
        elif loc is None:
            fail(f"Der Lagerort „{cells.get('lagerort')}“ ist unbekannt. Erlaubt: {', '.join(logic.LOCATIONS)}.")
        elif cells.get("packungsgroesse") and (pack is None or pack <= 0):
            fail(f"Die Packungsgröße von „{zutat}“ ist keine Zahl größer als 0.")
        else:
            u, factor = unit
            ing = rec["ingredients"].setdefault(zutat.casefold(), {
                "name": zutat, "qty": 0.0, "unit": u, "location": loc,
                "category": cells.get("kategorie", ""), "pack_size": pack * factor if pack else None,
            })
            if ing["unit"] and u and ing["unit"] != u:
                fail(f"„{zutat}“ kommt mit zwei Einheiten ({ing['unit']} und {u}) vor.")
            else:
                ing["unit"] = ing["unit"] or u
                ing["qty"] += qty * factor

    valid = [r for k, r in recipes.items() if k not in bad]
    for r in valid:
        r["ingredients"] = list(r["ingredients"].values())
        r["active"] = True if r["active"] is None else r["active"]
    for k in bad:
        errors.append(f"„{recipes[k]['name']}“ wurde nicht importiert.")
    if not recipes:
        raise CsvError("Die Datei enthält keine Rezepte.")
    return valid, errors


# ---------------------------------------------------------------------- Importieren

def problems(conn, rec) -> list:
    """Was gegen den Import dieses Rezepts spricht (Einheit passt nicht zu einer vorhandenen Zutat …)."""
    out = []
    for ing in rec["ingredients"]:
        p = logic.find_product(conn, ing["name"])
        if p and ing["unit"] and p["unit"] != ing["unit"]:
            out.append(f"„{p['name']}“ wird in {p['unit']} geführt, nicht in {ing['unit']}. "
                       "Einheit in der Datei anpassen oder in „Zutaten“ ändern.")
        elif not p and not ing["unit"]:
            out.append(f"„{ing['name']}“ ist neu und braucht eine Einheit.")
    return out


def apply(conn, recipes, overwrite=False) -> dict:
    """Legt Rezepte (und neue Zutaten) an. Vorhandene Rezepte gleichen Namens werden übersprungen
    oder – mit `overwrite` – ersetzt. Liefert {created, updated, skipped, errors}."""
    res = {"created": [], "updated": [], "skipped": [], "errors": []}
    for rec in recipes:
        existing = conn.execute("SELECT id FROM recipes WHERE name = ? COLLATE NOCASE", (rec["name"],)).fetchone()
        if existing and not overwrite:
            res["skipped"].append(rec["name"])
            continue
        # erst prüfen, dann anlegen: ein Rezept entsteht ganz oder gar nicht
        probs = problems(conn, rec)
        if probs:
            res["errors"] += [f"{rec['name']}: {m}" for m in probs] + [f"„{rec['name']}“ wurde nicht importiert."]
            continue
        ings = {}
        for ing in rec["ingredients"]:
            p = logic.find_product(conn, ing["name"])
            if p:
                pid = p["id"]
            else:
                pid = logic.create_product(conn, ing["name"], ing["unit"], ing["location"] or "Vorrat", ing["pack_size"])
                if ing["category"]:
                    conn.execute("UPDATE products SET category = ? WHERE id = ?", (ing["category"], pid))
            ings[pid] = ings.get(pid, 0) + ing["qty"]
        if existing:
            rid = existing["id"]
        else:
            rid = conn.execute("INSERT INTO recipes(name, kind, notes) VALUES(?, 'meal', '')", (rec["name"],)).lastrowid
        try:
            logic.update_recipe(conn, rid, rec["name"], rec["notes"], ings)
        except ValueError as e:
            if not existing:
                conn.execute("DELETE FROM recipes WHERE id = ?", (rid,))
            res["errors"].append(f"{rec['name']}: {e}")
            continue
        logic.set_recipe_active(conn, rid, rec.get("active", True))
        res["updated" if existing else "created"].append(rec["name"])
    conn.commit()
    return res
