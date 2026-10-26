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
