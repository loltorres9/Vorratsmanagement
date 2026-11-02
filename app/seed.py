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
IMPORT_KEY = "import_rezeptordner_v3"


def apply_recipe_import(conn, path: Path = IMPORT_FILE, seed_path: Path = SEED_FILE) -> bool:
    """Übernimmt die Rezepte aus dem Rezeptordner (einmalig, auch in bestehenden Datenbanken).

    Die Mengen stehen so da wie im Rezept und gelten für eine Portion; es kommt nur hinein, was
    im Rezept steht. Rezepte, die in der App bearbeitet wurden (`user_edited`), bleiben unberührt.
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

    def write(rid, ings):
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (rid,))
        for name, qty in ings.items():
            if name not in pid:
                raise ValueError(f"Rezeptordner: unbekanntes Produkt '{name}'")
            conn.execute(
                "INSERT INTO recipe_ingredients(recipe_id, product_id, qty) VALUES(?,?,?)",
                (rid, pid[name], qty),
            )

    def find(names):
        for n in names:
            row = conn.execute("SELECT id, user_edited FROM recipes WHERE name = ?", (n,)).fetchone()
            if row:
                return row
        return None

    for r in data["recipes"]:
        row = find(r["names"])
        if not row or row["user_edited"]:
            continue
        conn.execute("UPDATE recipes SET name = ?, notes = ? WHERE id = ?", (r["name"], r["notes"], row["id"]))
        write(row["id"], r["ingredients"])

    old_names = {}
    for old, new in data.get("renamed", {}).items():
        old_names.setdefault(new, []).append(old)
    for r in data["new_recipes"]:
        row = find([r["name"]] + old_names.get(r["name"], []))
        if row and row["user_edited"]:
            continue
        if row:
            conn.execute("UPDATE recipes SET name = ?, notes = ? WHERE id = ?", (r["name"], r["notes"], row["id"]))
            rid = row["id"]
        else:
            rid = conn.execute("INSERT INTO recipes(name, kind, notes) VALUES(?, 'meal', '')", (r["name"],)).lastrowid
            conn.execute("UPDATE recipes SET notes = ? WHERE id = ?", (r["notes"], rid))
        write(rid, r["ingredients"])

    # Gulasch-Gerichte: der frühere Import hatte die Knödel hineingeschrieben; zurück auf den Startstand.
    seed_recipes = {x["name"]: x for x in json.loads(Path(seed_path).read_text(encoding="utf-8"))["recipes"]}
    for name in data.get("restore_from_seed", []):
        row = find([name])
        if not row or row["user_edited"] or name not in seed_recipes:
            continue
        conn.execute("UPDATE recipes SET notes = '' WHERE id = ?", (row["id"],))
        write(row["id"], seed_recipes[name]["ingredients"])

    # Portionen gibt es nicht mehr: Die Mengen eines Rezepts gelten immer für eine Portion.
    conn.execute("UPDATE recipes SET servings = 1")
    db.set_setting(conn, IMPORT_KEY, "1")
    conn.commit()
    return True
