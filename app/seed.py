"""Lädt seed/seed.json in eine leere Datenbank."""
import json
from pathlib import Path

from . import db

SEED_FILE = Path(__file__).resolve().parent.parent / "seed" / "seed.json"


def is_empty(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def load(conn, path: Path = SEED_FILE) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pid = {}
    for p in data["products"]:
        cur = conn.execute(
            "INSERT INTO products(name, unit, location, category, min_stock, pack_size, price) "
            "VALUES(?,?,?,?,?,?,?)",
            (p["name"], p["unit"], p["location"], p.get("category", ""),
             p.get("min_stock", 0), p.get("pack_size"), p.get("price")),
        )
        pid[p["name"]] = cur.lastrowid

    def add_recipe(r, kind):
        yields = r.get("yields")
        cur = conn.execute(
            "INSERT INTO recipes(name, kind, yields_product_id, yields_qty, notes) VALUES(?,?,?,?,?)",
            (r["name"], kind,
             pid[yields["product"]] if yields else None,
             yields["qty"] if yields else None,
             r.get("notes", "")),
        )
        rid = cur.lastrowid
        for name, qty in r["ingredients"].items():
            if name not in pid:
                raise ValueError(f"Rezept '{r['name']}': unbekanntes Produkt '{name}'")
            conn.execute(
                "INSERT INTO recipe_ingredients(recipe_id, product_id, qty) VALUES(?,?,?)",
                (rid, pid[name], qty),
            )
        return rid

    rid_by_name = {}
    for r in data.get("batch_recipes", []):
        rid_by_name[r["name"]] = add_recipe(r, "batch")
    for r in data["recipes"]:
        rid = add_recipe(r, "meal")
        rid_by_name[r["name"]] = rid
        if r.get("rotation"):
            week, weekday = r["rotation"]
            conn.execute(
                "INSERT INTO rotation(week, weekday, recipe_id) VALUES(?,?,?)", (week, weekday, rid)
            )

    for p in data["products"]:
        if p.get("made_by"):
            conn.execute(
                "UPDATE products SET made_by_recipe_id = ? WHERE id = ?",
                (rid_by_name[p["made_by"]], pid[p["name"]]),
            )

    db.set_setting(conn, "rotation_weeks", data.get("rotation_weeks", 5))
    conn.commit()


def ensure_seeded(conn) -> bool:
    if is_empty(conn):
        load(conn)
        return True
    return False


# ------------------------------------------------------------------ Rezeptordner

IMPORT_FILE = SEED_FILE.parent / "rezeptordner.json"
IMPORT_KEY = "import_rezeptordner_v1"


def apply_recipe_import(conn, path: Path = IMPORT_FILE) -> bool:
    """Übernimmt die Rezepte aus dem Rezeptordner (einmalig, auch in bestehenden Datenbanken).

    Bestehende Gerichte werden anhand ihres Namens gefunden; ihre Zutaten werden ersetzt.
    Ein Lauf ohne passenden Namen (z. B. weil das Rezept umbenannt wurde) überspringt es.
    """
    if db.get_setting(conn, IMPORT_KEY):
        return False
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    for p in data["products"]:
        conn.execute(
            "INSERT OR IGNORE INTO products(name, unit, location, category, pack_size) VALUES(?,?,?,?,?)",
            (p["name"], p["unit"], p["location"], p.get("category", ""), p.get("pack_size")),
        )
    pid = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM products")}

    def per_portion(r, current):
        ings = {name: qty for name, qty in current.items() if name in r.get("keep", [])}
        for name, total in r["ingredients"].items():
            ings[name] = round(total / r["servings"], 3)
        ings.update(r.get("per_portion", {}))
        return ings

    def write(rid, ings):
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (rid,))
        for name, qty in ings.items():
            if name not in pid:
                raise ValueError(f"Rezeptordner: unbekanntes Produkt '{name}'")
            conn.execute(
                "INSERT INTO recipe_ingredients(recipe_id, product_id, qty) VALUES(?,?,?)",
                (rid, pid[name], qty),
            )

    for r in data["recipes"]:
        row = conn.execute("SELECT id FROM recipes WHERE name = ?", (r["match"],)).fetchone()
        if not row:
            continue
        current = {
            x["name"]: x["qty"]
            for x in conn.execute(
                "SELECT p.name, ri.qty FROM recipe_ingredients ri JOIN products p ON p.id = ri.product_id "
                "WHERE ri.recipe_id = ?", (row["id"],))
        }
        conn.execute(
            "UPDATE recipes SET name = ?, notes = ? WHERE id = ?",
            (r.get("rename", r["match"]), r["notes"], row["id"]),
        )
        write(row["id"], per_portion(r, current))

    for r in data["new_recipes"]:
        if conn.execute("SELECT 1 FROM recipes WHERE name = ?", (r["name"],)).fetchone():
            continue
        cur = conn.execute("INSERT INTO recipes(name, kind, notes) VALUES(?, 'meal', ?)", (r["name"], r["notes"]))
        write(cur.lastrowid, per_portion(r, {}))

    db.set_setting(conn, IMPORT_KEY, "1")
    conn.commit()
    return True
