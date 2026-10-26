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
IMPORT_KEY = "import_rezeptordner_v2"


def apply_recipe_import(conn, path: Path = IMPORT_FILE) -> bool:
    """Übernimmt die Rezepte aus dem Rezeptordner (einmalig, auch in bestehenden Datenbanken).

    Die Mengen werden wie im Rezept gespeichert (für `servings` Portionen). Bestehende
    Gerichte werden anhand ihres Namens gefunden (`match` oder `rename`); ihre Zutaten
    werden ersetzt, Beilagen aus `keep` bleiben mit ihrer bisherigen Menge pro Portion.
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

    def write(rid, r, current):
        n = r["servings"]
        ings = {name: qty * n for name, qty in current.items() if name in r.get("keep", [])}
        ings.update(r["ingredients"])
        ings.update({name: qty * n for name, qty in r.get("per_portion", {}).items()})
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (rid,))
        for name, qty in ings.items():
            if name not in pid:
                raise ValueError(f"Rezeptordner: unbekanntes Produkt '{name}'")
            conn.execute(
                "INSERT INTO recipe_ingredients(recipe_id, product_id, qty) VALUES(?,?,?)",
                (rid, pid[name], round(qty, 3)),
            )

    def per_portion_now(rid):
        return {
            x["name"]: x["per_portion"]
            for x in conn.execute(
                "SELECT p.name, ri.qty / r.servings AS per_portion FROM recipe_ingredients ri "
                "JOIN products p ON p.id = ri.product_id JOIN recipes r ON r.id = ri.recipe_id "
                "WHERE ri.recipe_id = ?", (rid,))
        }

    for r in data["recipes"]:
        row = conn.execute(
            "SELECT id, user_edited FROM recipes WHERE name IN (?, ?)",
            (r["match"], r.get("rename", r["match"])),
        ).fetchone()
        if not row or row["user_edited"]:
            continue
        current = per_portion_now(row["id"])
        conn.execute(
            "UPDATE recipes SET name = ?, notes = ?, servings = ? WHERE id = ?",
            (r.get("rename", r["match"]), r["notes"], r["servings"], row["id"]),
        )
        write(row["id"], r, current)

    for r in data["new_recipes"]:
        row = conn.execute("SELECT id, user_edited FROM recipes WHERE name = ?", (r["name"],)).fetchone()
        if row and row["user_edited"]:
            continue
        if row:
            conn.execute("UPDATE recipes SET notes = ?, servings = ? WHERE id = ?",
                         (r["notes"], r["servings"], row["id"]))
            rid = row["id"]
        else:
            rid = conn.execute(
                "INSERT INTO recipes(name, kind, notes, servings) VALUES(?, 'meal', ?, ?)",
                (r["name"], r["notes"], r["servings"]),
            ).lastrowid
        write(rid, r, {})

    db.set_setting(conn, IMPORT_KEY, "1")
    conn.commit()
    return True


FIX_DAMPFNUDELN_KEY = "fix_dampfnudeln_gekauft_v1"


def apply_dampfnudeln_fix(conn) -> bool:
    """Dampfnudeln werden gekauft (gekühlt), nicht selbst gemacht: zurück auf 2 Stück + Vanillesoße pro Portion."""
    if db.get_setting(conn, FIX_DAMPFNUDELN_KEY):
        return False
    row = conn.execute("SELECT id, user_edited FROM recipes WHERE name = 'Dampfnudeln mit Vanillesoße'").fetchone()
    prods = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM products WHERE name IN ('Dampfnudeln', 'Vanillesoße')")}
    if row and not row["user_edited"] and len(prods) == 2:
        conn.execute("UPDATE recipes SET servings = 1, notes = '' WHERE id = ?", (row["id"],))
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (row["id"],))
        conn.executemany(
            "INSERT INTO recipe_ingredients(recipe_id, product_id, qty) VALUES(?,?,?)",
            [(row["id"], prods["Dampfnudeln"], 2), (row["id"], prods["Vanillesoße"], 200)],
        )
    db.set_setting(conn, FIX_DAMPFNUDELN_KEY, "1")
    conn.commit()
    return True
