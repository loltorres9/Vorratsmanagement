"""Kernlogik: Speiseplan, Bedarf, Einkaufsliste, Bestand.

Alle Funktionen bekommen eine sqlite3-Connection und committen selbst.
"""
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import db

EPS = 1e-9
LOCATION_ORDER = {"Vorrat": 0, "Kühlschrank": 1, "TK": 2}


# --------------------------------------------------------------------------- Zeit

def today() -> date:
    tz = os.environ.get("TZ_NAME", "Europe/Berlin")
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    return date.today()


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# --------------------------------------------------------------------- Formatierung

def fmt_qty(qty, unit) -> str:
    if qty is None:
        return ""
    if unit == "g" and qty >= 1000:
        qty, unit = qty / 1000, "kg"
    elif unit == "ml" and qty >= 1000:
        qty, unit = qty / 1000, "l"
    if abs(qty - round(qty)) < 0.01:
        s = str(int(round(qty)))
    else:
        s = f"{qty:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{s} {unit}"


def item_text(item) -> str:
    """Text, wie er in Google Keep steht."""
    if item["qty"] is None:
        return item["name"]
    q = fmt_qty(item["qty"], item["unit"])
    if item["packs"] and item["packs"] > 1:
        return f"{item['name']} – {item['packs']}× ({q})"
    return f"{item['name']} – {q}"


# ------------------------------------------------------------------------ Bestand

def change_stock(conn, product_id, delta, reason, commit=True):
    row = conn.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()
    new = max(0.0, row["stock"] + delta)
    conn.execute("UPDATE products SET stock = ? WHERE id = ?", (new, product_id))
    conn.execute(
        "INSERT INTO stock_log(product_id, delta, reason) VALUES(?,?,?)",
        (product_id, new - row["stock"], reason),
    )
    if commit:
        conn.commit()


def set_stock(conn, product_id, value, reason="Inventur"):
    row = conn.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()
    change_stock(conn, product_id, float(value) - row["stock"], reason)


# --------------------------------------------------------------------- Speiseplan

def rotation_start(conn) -> date:
    v = db.get_setting(conn, "rotation_start") or os.environ.get("ROTATION_START")
    if not v:
        v = monday_of(today()).isoformat()
        db.set_setting(conn, "rotation_start", v)
        conn.commit()
    return monday_of(date.fromisoformat(v))


def rotation_recipe(conn, d: date):
    weeks = int(db.get_setting(conn, "rotation_weeks", 5))
    week = ((d - rotation_start(conn)).days // 7) % weeks + 1
    row = conn.execute(
        "SELECT recipe_id FROM rotation WHERE week = ? AND weekday = ?", (week, d.weekday())
    ).fetchone()
    return (row["recipe_id"] if row else None), week


def ensure_plan(conn, start: date | None = None, days: int = 14):
    """Füllt fehlende Tage aus der Rotation auf. Bestehende Einträge bleiben unangetastet."""
    start = start or today()
    for i in range(days):
        d = start + timedelta(days=i)
        rid, _ = rotation_recipe(conn, d)
        if rid:
            conn.execute(
                "INSERT OR IGNORE INTO meal_plan(date, recipe_id) VALUES(?, ?)", (d.isoformat(), rid)
            )
    conn.commit()


def plan(conn, start: date | None = None, days: int = 14):
    start = start or today()
    end = start + timedelta(days=days)
    return conn.execute(
        "SELECT m.*, r.name AS recipe_name FROM meal_plan m JOIN recipes r ON r.id = m.recipe_id "
        "WHERE m.date >= ? AND m.date < ? ORDER BY m.date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def set_plan(conn, d: str, recipe_id: int, portions: float = 1):
    conn.execute(
        "INSERT INTO meal_plan(date, recipe_id, portions, status) VALUES(?,?,?, 'planned') "
        "ON CONFLICT(date) DO UPDATE SET recipe_id = excluded.recipe_id, "
        "portions = excluded.portions, status = 'planned'",
        (d, recipe_id, portions),
    )
    conn.commit()


def ingredients(conn, recipe_id):
    return conn.execute(
        "SELECT ri.qty, p.* FROM recipe_ingredients ri JOIN products p ON p.id = ri.product_id "
        "WHERE ri.recipe_id = ? ORDER BY p.location, p.name",
        (recipe_id,),
    ).fetchall()


def cook_recipe(conn, recipe_id, portions=1.0, reason=None):
    """Zieht Zutaten ab; Batch-Rezepte buchen das Ergebnis (z.B. 4 Gulasch-Portionen) ein."""
    r = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    reason = reason or f"Gekocht: {r['name']}"
    for ing in ingredients(conn, recipe_id):
        change_stock(conn, ing["id"], -ing["qty"] * portions, reason, commit=False)
    if r["kind"] == "batch" and r["yields_product_id"]:
        change_stock(conn, r["yields_product_id"], (r["yields_qty"] or 1) * portions, reason, commit=False)
    conn.commit()


def mark_cooked(conn, d: str):
    m = conn.execute("SELECT * FROM meal_plan WHERE date = ?", (d,)).fetchone()
    if not m or m["status"] == "cooked":
        return
    cook_recipe(conn, m["recipe_id"], m["portions"])
    conn.execute("UPDATE meal_plan SET status = 'cooked' WHERE date = ?", (d,))
    conn.commit()


def mark_skipped(conn, d: str):
    conn.execute("UPDATE meal_plan SET status = 'skipped' WHERE date = ? AND status = 'planned'", (d,))
    conn.commit()


# ---------------------------------------------------------------------- Bedarf

def requirements(conn, start: date, days: int):
    """Summe der Zutaten aller noch geplanten Gerichte im Zeitraum (product_id -> Menge)."""
    req = defaultdict(float)
    end = start + timedelta(days=days)
    rows = conn.execute(
        "SELECT m.portions, ri.product_id, ri.qty FROM meal_plan m "
        "JOIN recipe_ingredients ri ON ri.recipe_id = m.recipe_id "
        "WHERE m.status = 'planned' AND m.date >= ? AND m.date < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    for r in rows:
        req[r["product_id"]] += r["qty"] * r["portions"]
    return req


def compute_needs(conn, start: date | None = None, days: int | None = None):
    """Was muss gekauft werden? Liste von dicts, sortiert nach Laden-Reihenfolge.

    Bedarf = Zutaten der nächsten `days` Tage + Mindestbestand − Bestand.
    Selbstgemachte Produkte (made_by_recipe_id) werden in die Zutaten ihres
    Batch-Rezepts aufgelöst, statt selbst auf der Liste zu landen.
    """
    start = start or today()
    days = days or int(os.environ.get("PLAN_DAYS", 7))
    products = {p["id"]: p for p in conn.execute("SELECT * FROM products WHERE active = 1")}
    req = requirements(conn, start, days)
    hints = []

    for p in list(products.values()):
        if not p["made_by_recipe_id"]:
            continue
        deficit = req.get(p["id"], 0) + p["min_stock"] - p["stock"]
        if deficit > EPS:
            r = conn.execute("SELECT * FROM recipes WHERE id = ?", (p["made_by_recipe_id"],)).fetchone()
            batches = math.ceil(deficit / (r["yields_qty"] or 1))
            for ing in ingredients(conn, r["id"]):
                req[ing["id"]] += ing["qty"] * batches
            hints.append(f"{r['name']}: {batches}× kochen")

    needs = []
    for pid, p in products.items():
        if p["made_by_recipe_id"]:
            continue
        need = req.get(pid, 0) + p["min_stock"] - p["stock"]
        if need <= EPS:
            continue
        packs = math.ceil(need / p["pack_size"] - EPS) if p["pack_size"] else None
        qty = packs * p["pack_size"] if packs else need
        needs.append({
            "product_id": pid, "name": p["name"], "unit": p["unit"], "qty": qty,
            "packs": packs, "need": need, "location": p["location"], "category": p["category"],
        })
    needs.sort(key=lambda n: (LOCATION_ORDER.get(n["location"], 9), n["category"], n["name"]))
    return needs, hints


# ----------------------------------------------------------------- Einkaufsliste

def refresh_shopping_list(conn, start: date | None = None, days: int | None = None):
    """Gleicht die automatischen Einträge mit dem aktuellen Bedarf ab.

    Bestehende Einträge werden aktualisiert statt neu angelegt, damit die
    Verknüpfung zu Google Keep erhalten bleibt.
    """
    needs, hints = compute_needs(conn, start, days)
    open_rows = conn.execute("SELECT * FROM shopping_items WHERE status = 'open'").fetchall()
    auto = {r["product_id"]: r for r in open_rows if r["source"] == "auto"}
    manual_pids = {r["product_id"] for r in open_rows if r["source"] != "auto" and r["product_id"]}
    wanted = set()

    for n in needs:
        pid = n["product_id"]
        if pid in manual_pids:
            continue
        wanted.add(pid)
        if pid in auto:
            conn.execute(
                "UPDATE shopping_items SET qty = ?, packs = ?, updated_at = datetime('now') WHERE id = ?",
                (n["qty"], n["packs"], auto[pid]["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO shopping_items(product_id, name, qty, unit, packs, source) "
                "VALUES(?,?,?,?,?, 'auto')",
                (pid, n["name"], n["qty"], n["unit"], n["packs"]),
            )

    for pid, row in auto.items():
        if pid not in wanted:
            if row["keep_item_id"]:
                # Keep-Sync entfernt den Eintrag beim nächsten Lauf
                conn.execute("UPDATE shopping_items SET status = 'dismissed' WHERE id = ?", (row["id"],))
            else:
                conn.execute("DELETE FROM shopping_items WHERE id = ?", (row["id"],))
    conn.commit()
    return hints


def shopping_list(conn, status="open"):
    return conn.execute(
        "SELECT s.*, COALESCE(p.location, 'Sonstiges') AS location, COALESCE(p.category, '') AS category "
        "FROM shopping_items s LEFT JOIN products p ON p.id = s.product_id "
        "WHERE s.status = ? ORDER BY "
        "CASE COALESCE(p.location,'') WHEN 'Vorrat' THEN 0 WHEN 'Kühlschrank' THEN 1 WHEN 'TK' THEN 2 ELSE 3 END, "
        "category, s.name",
        (status,),
    ).fetchall()


def find_product(conn, name):
    return conn.execute("SELECT * FROM products WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()


def add_manual(conn, name, qty=None, unit=None, source="manual", keep_item_id=None):
    p = find_product(conn, name)
    if p and qty is not None and unit is None:
        unit = p["unit"]
    cur = conn.execute(
        "INSERT INTO shopping_items(product_id, name, qty, unit, source, keep_item_id) VALUES(?,?,?,?,?,?)",
        (p["id"] if p else None, p["name"] if p else name.strip(), qty, unit, source, keep_item_id),
    )
    conn.commit()
    return cur.lastrowid


def mark_bought(conn, item_id, commit=True):
    it = conn.execute("SELECT * FROM shopping_items WHERE id = ?", (item_id,)).fetchone()
    if not it or it["status"] != "open":
        return
    conn.execute(
        "UPDATE shopping_items SET status = 'bought', updated_at = datetime('now') WHERE id = ?", (item_id,)
    )
    if it["product_id"] and it["qty"]:
        change_stock(conn, it["product_id"], it["qty"], "Eingekauft", commit=False)
    if commit:
        conn.commit()


def dismiss(conn, item_id, commit=True):
    conn.execute(
        "UPDATE shopping_items SET status = 'dismissed', updated_at = datetime('now') WHERE id = ?", (item_id,)
    )
    if commit:
        conn.commit()


def week_cost(conn):
    """Grobe Kosten der aktuellen Einkaufsliste (nur Produkte mit Preis)."""
    row = conn.execute(
        "SELECT SUM(COALESCE(s.packs, 1) * p.price) AS total, COUNT(p.price) AS priced, COUNT(*) AS n "
        "FROM shopping_items s JOIN products p ON p.id = s.product_id WHERE s.status = 'open'"
    ).fetchone()
    return row
