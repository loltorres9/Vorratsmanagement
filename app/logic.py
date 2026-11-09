"""Kernlogik: Speiseplan, Bedarf, Einkaufsliste, Bestand.

Alle Funktionen bekommen eine sqlite3-Connection und committen selbst.
"""
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import db

EPS = 1e-9
LOCATION_ORDER = {"Vorrat": 0, "Kühlschrank": 1, "TK": 2}
UNITS = ("g", "ml", "Stk", "Portion", "Tasse", "EL", "TL", "Packung")
LOCATIONS = tuple(LOCATION_ORDER)

# Grobe Gewichte für das Tiefkühlfach (g pro Einheit; g und ml zählen 1:1).
UNIT_G = {"Portion": 400, "Stk": 100, "Packung": 400, "Tasse": 200, "EL": 15, "TL": 5}


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
        "SELECT ro.recipe_id FROM rotation ro JOIN recipes r ON r.id = ro.recipe_id "
        "WHERE ro.week = ? AND ro.weekday = ? AND r.active = 1",
        (week, d.weekday()),
    ).fetchone()
    return (row["recipe_id"] if row else None), week


def build_rotation(conn, rng=None) -> tuple:
    """Verteilt alle aktiven Gerichte (kind meal) auf eine neue Rotation und ersetzt die alte.

    Wochen = aufgerundet (Gerichte / 7), jede Woche hat 7 Tage; bleiben Plätze übrig, wiederholen sich
    Gerichte. Woche 1 ist die aktuelle Woche. Noch nicht gekochte Tage ab heute werden verworfen und
    aus der neuen Rotation neu gefüllt; Gekochtes bleibt. Liefert (Wochen, Gerichte).
    Wirft ValueError, wenn es kein aktives Gericht gibt."""
    import random
    rng = rng or random.Random()
    ids = [r["id"] for r in conn.execute("SELECT id FROM recipes WHERE kind = 'meal' AND active = 1 ORDER BY name")]
    if not ids:
        raise ValueError("Es gibt kein aktives Gericht, das in die Rotation passt.")
    weeks = math.ceil(len(ids) / 7)
    order = ids[:]
    rng.shuffle(order)
    slots = weeks * 7
    order += rng.sample(ids, min(len(ids), slots - len(order))) if len(order) < slots else []
    while len(order) < slots:  # weniger als 7 Gerichte: durchlaufend wiederholen
        order.append(ids[len(order) % len(ids)])
    conn.execute("DELETE FROM rotation")
    conn.executemany("INSERT INTO rotation(week, weekday, recipe_id) VALUES(?,?,?)",
                     [(i // 7 + 1, i % 7, rid) for i, rid in enumerate(order)])
    db.set_setting(conn, "rotation_weeks", weeks)
    db.set_setting(conn, "rotation_start", monday_of(today()).isoformat())
    conn.execute("DELETE FROM meal_plan WHERE status IN ('planned', 'covered') AND date >= ?", (today().isoformat(),))
    conn.commit()
    ensure_plan(conn, days=21)
    return weeks, len(ids)


def rotation_size(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM rotation").fetchone()[0]


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
    # span = Zahl der Tage, die das Gericht reicht; day_no = der wievielte Tag davon (1 = es wird gekocht)
    return conn.execute(
        "SELECT m.*, r.name AS recipe_name, "
        "(SELECT COUNT(*) FROM meal_plan c WHERE c.anchor_date = COALESCE(m.anchor_date, m.date)) + 1 AS span, "
        "CAST(julianday(m.date) - julianday(COALESCE(m.anchor_date, m.date)) AS INTEGER) + 1 AS day_no "
        "FROM meal_plan m JOIN recipes r ON r.id = m.recipe_id "
        "WHERE m.date >= ? AND m.date < ? ORDER BY m.date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


MAX_COVER_DAYS = 7


def _release_coverage(conn, anchor: str):
    """Gibt die Reste-Tage eines Gerichts frei (ensure_plan belegt sie wieder aus der Rotation)."""
    conn.execute("DELETE FROM meal_plan WHERE anchor_date = ? AND status = 'covered'", (anchor,))


def _shrink_anchor(conn, d: str):
    """Wird ein Reste-Tag anders belegt, reicht das Gericht nur bis zum Vortag; spätere Reste-Tage werden frei."""
    row = conn.execute("SELECT anchor_date FROM meal_plan WHERE date = ? AND status = 'covered'", (d,)).fetchone()
    if not row:
        return
    anchor = row["anchor_date"]
    kept = (date.fromisoformat(d) - date.fromisoformat(anchor)).days
    conn.execute("DELETE FROM meal_plan WHERE anchor_date = ? AND date >= ?", (anchor, d))
    conn.execute("UPDATE meal_plan SET portions = ? WHERE date = ? AND status = 'planned'", (kept, anchor))


def set_plan(conn, d: str, recipe_id: int, portions: float = 1):
    _shrink_anchor(conn, d)
    _release_coverage(conn, d)
    conn.execute(
        "INSERT INTO meal_plan(date, recipe_id, portions, status, anchor_date) VALUES(?,?,?, 'planned', NULL) "
        "ON CONFLICT(date) DO UPDATE SET recipe_id = excluded.recipe_id, "
        "portions = excluded.portions, status = 'planned', anchor_date = NULL",
        (d, recipe_id, portions),
    )
    conn.commit()


def set_coverage(conn, d: str, days: int):
    """Das Gericht am Tag `d` reicht für `days` Tage: Es wird einmal für `days` Portionen gekocht, die
    Folgetage bleiben als „Reste“ ohne eigenes Gericht und zählen nicht zum Bedarf. days = 1 hebt das auf.

    Wirft ValueError, wenn der Tag nicht (mehr) geplant ist oder ein Folgetag schon gekocht wurde."""
    if not 1 <= days <= MAX_COVER_DAYS:
        raise ValueError(f"Ein Gericht kann für 1 bis {MAX_COVER_DAYS} Tage reichen.")
    m = conn.execute("SELECT * FROM meal_plan WHERE date = ?", (d,)).fetchone()
    if not m or m["status"] != "planned":
        raise ValueError("Nur ein noch geplantes Gericht kann für mehrere Tage reichen.")
    start = date.fromisoformat(d)
    following = [(start + timedelta(days=i)).isoformat() for i in range(1, days)]
    for f in following:
        row = conn.execute("SELECT status FROM meal_plan WHERE date = ?", (f,)).fetchone()
        if row and row["status"] == "cooked":
            raise ValueError(f"Am {date.fromisoformat(f).strftime('%d.%m.')} ist schon gekocht.")
    _release_coverage(conn, d)
    for f in following:
        _shrink_anchor(conn, f)       # ein Gericht, das in diesen Tag hineinreichte, endet am Vortag
        _release_coverage(conn, f)    # liegt hier selbst ein Gericht mit Resten, werden die frei
        conn.execute(
            "INSERT OR REPLACE INTO meal_plan(date, recipe_id, portions, status, anchor_date) "
            "VALUES(?,?,0,'covered',?)", (f, m["recipe_id"], d))
    conn.execute("UPDATE meal_plan SET portions = ? WHERE date = ?", (days, d))
    conn.commit()


def ingredients(conn, recipe_id):
    return conn.execute(
        "SELECT ri.qty, ri.qty / r.servings AS per_portion, p.* FROM recipe_ingredients ri "
        "JOIN products p ON p.id = ri.product_id JOIN recipes r ON r.id = ri.recipe_id "
        "WHERE ri.recipe_id = ? ORDER BY p.location, p.name",
        (recipe_id,),
    ).fetchall()


def set_recipe_active(conn, recipe_id, active: bool):
    """Inaktive Gerichte verschwinden aus der Rotation und aus dem noch ungekochten Plan."""
    conn.execute("UPDATE recipes SET active = ? WHERE id = ?", (1 if active else 0, recipe_id))
    if not active:
        conn.execute(
            "DELETE FROM meal_plan WHERE recipe_id = ? AND status IN ('planned', 'covered') AND date >= ?",
            (recipe_id, today().isoformat()),
        )
    conn.commit()


def update_recipe(conn, recipe_id, name, notes, ingredients, yields_qty=None, servings=None):
    """Name, Notiz, Portionen und Zutaten (product_id -> Menge laut Rezept) ersetzen.

    Wirft ValueError bei ungültigen Angaben."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Der Name fehlt.")
    if not ingredients:
        raise ValueError("Ein Rezept braucht mindestens eine Zutat.")
    if any(q <= 0 for q in ingredients.values()):
        raise ValueError("Mengen müssen größer als 0 sein.")
    if servings is not None and servings <= 0:
        raise ValueError("Die Portionen müssen größer als 0 sein.")
    try:
        conn.execute(
            "UPDATE recipes SET name = ?, notes = ?, yields_qty = COALESCE(?, yields_qty), "
            "servings = COALESCE(?, servings), user_edited = 1 WHERE id = ?",
            (name, (notes or "").strip(), yields_qty, servings, recipe_id),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"Ein Rezept „{name}“ gibt es schon.")
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    conn.executemany(
        "INSERT INTO recipe_ingredients(recipe_id, product_id, qty) VALUES(?,?,?)",
        [(recipe_id, pid, q) for pid, q in ingredients.items()],
    )
    conn.commit()


def delete_all_recipes(conn) -> int:
    """Löscht alle Rezepte samt Rotation und Plan-Einträgen. Rezepte, aus denen ein Produkt
    selbst gemacht wird (made_by_recipe_id, z. B. das Gulasch-Batch), bleiben, sonst verlöre
    das Produkt seine Herkunft. Bestand, Zutaten und Einkaufsliste bleiben unberührt.
    Liefert die Zahl der gelöschten Rezepte."""
    keep = {r[0] for r in conn.execute("SELECT made_by_recipe_id FROM products WHERE made_by_recipe_id IS NOT NULL")}
    ids = [(r["id"],) for r in conn.execute("SELECT id FROM recipes") if r["id"] not in keep]
    conn.executemany("DELETE FROM rotation WHERE recipe_id = ?", ids)
    conn.executemany("DELETE FROM meal_plan WHERE recipe_id = ?", ids)
    conn.executemany("DELETE FROM recipes WHERE id = ?", ids)
    conn.commit()
    return len(ids)


def cook_recipe(conn, recipe_id, portions=1.0, reason=None):
    """Zieht Zutaten ab; Batch-Rezepte buchen das Ergebnis (z.B. 4 Gulasch-Portionen) ein."""
    r = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    reason = reason or f"Gekocht: {r['name']}"
    for ing in ingredients(conn, recipe_id):
        change_stock(conn, ing["id"], -ing["per_portion"] * portions, reason, commit=False)
    if r["kind"] == "batch" and r["yields_product_id"]:
        change_stock(conn, r["yields_product_id"], (r["yields_qty"] or 1) * portions, reason, commit=False)
    conn.commit()


def mark_cooked(conn, d: str):
    m = conn.execute("SELECT * FROM meal_plan WHERE date = ?", (d,)).fetchone()
    if not m or m["status"] in ("cooked", "covered"):
        return
    cook_recipe(conn, m["recipe_id"], m["portions"])
    conn.execute("UPDATE meal_plan SET status = 'cooked' WHERE date = ?", (d,))
    conn.commit()


def mark_skipped(conn, d: str):
    row = conn.execute("SELECT status FROM meal_plan WHERE date = ?", (d,)).fetchone()
    if row and row["status"] == "planned":
        _release_coverage(conn, d)
        conn.execute("UPDATE meal_plan SET status = 'skipped', portions = 1 WHERE date = ?", (d,))
    conn.commit()


# ---------------------------------------------------------------------- Bedarf

def requirements(conn, start: date, days: int):
    """Summe der Zutaten aller noch geplanten Gerichte im Zeitraum (product_id -> Menge)."""
    req = defaultdict(float)
    end = start + timedelta(days=days)
    rows = conn.execute(
        "SELECT m.portions, ri.product_id, ri.qty / r.servings AS qty FROM meal_plan m "
        "JOIN recipe_ingredients ri ON ri.recipe_id = m.recipe_id "
        "JOIN recipes r ON r.id = m.recipe_id "
        "WHERE m.status = 'planned' AND m.date >= ? AND m.date < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    for r in rows:
        req[r["product_id"]] += r["qty"] * r["portions"]
    return req


def _weight_g(unit, qty):
    """Grobes Gewicht für die Tiefkühlfach-Rechnung."""
    return qty * UNIT_G.get(unit, 1)


def compute_needs(conn, start: date | None = None, days: int | None = None,
                  reserve_days: int | None = None, tk_capacity_g: float | None = None):
    """Was muss gekauft werden? Liste von dicts, sortiert nach Laden-Reihenfolge.

    Bedarf = Zutaten der nächsten `days` Tage + Mindestbestand − Bestand.
    Haltbare Produkte (Vorrat, TK) werden zusätzlich für `reserve_days` Tage
    (Standard 10, Umgebungsvariable RESERVE_DAYS) vorgehalten, Frischware nicht.
    Passt die TK-Reserve nicht ins Tiefkühlfach (`tk_capacity_g`, Standard 4000 g,
    Umgebungsvariable TK_CAPACITY_G), wird ihr Zeitraum verkürzt, mindestens auf `days`.
    Selbstgemachte Produkte (made_by_recipe_id) werden in die Zutaten ihres
    Batch-Rezepts aufgelöst, statt selbst auf der Liste zu landen.
    """
    start = start or today()
    days = days or int(os.environ.get("PLAN_DAYS", 7))
    reserve_days = int(os.environ.get("RESERVE_DAYS", 10)) if reserve_days is None else reserve_days
    cap = float(os.environ.get("TK_CAPACITY_G", 4000)) if tk_capacity_g is None else tk_capacity_g
    reserve_days = max(reserve_days, days)
    for tk_days in range(reserve_days, days - 1, -1):
        needs, hints = _needs(conn, start, days, reserve_days, tk_days)
        tk_total = _tk_weight(conn, needs)
        if cap <= 0 or tk_total <= cap or tk_days == days:
            break
    if tk_days < reserve_days:
        msg = f"Tiefkühlfach voll: TK-Reserve nur für {tk_days} statt {reserve_days} Tage"
        if tk_total > cap > 0:
            msg += f" (auch das übersteigt {fmt_qty(cap, 'g')})"
        hints.append(msg)
    return needs, hints


def _tk_weight(conn, needs):
    """Gewicht im Tiefkühlfach, wenn alles Eingekaufte drin liegt."""
    bought = {n["product_id"]: n["qty"] for n in needs}
    total = 0.0
    for p in conn.execute("SELECT * FROM products WHERE active = 1 AND location = 'TK'"):
        total += _weight_g(p["unit"], p["stock"] + bought.get(p["id"], 0))
    return total


def _needs(conn, start, days, reserve_days, tk_days):
    products = {p["id"]: p for p in conn.execute("SELECT * FROM products WHERE active = 1")}
    req = requirements(conn, start, days)
    for horizon, locations in ((reserve_days, ("Vorrat",)), (tk_days, ("TK",))):
        if horizon > days:
            for pid, qty in requirements(conn, start, horizon).items():
                if products.get(pid) and products[pid]["location"] in locations:
                    req[pid] = qty
    hints = []

    for p in list(products.values()):
        if not p["made_by_recipe_id"]:
            continue
        deficit = req.get(p["id"], 0) + p["min_stock"] - p["stock"]
        if deficit > EPS:
            r = conn.execute("SELECT * FROM recipes WHERE id = ?", (p["made_by_recipe_id"],)).fetchone()
            batches = math.ceil(deficit / (r["yields_qty"] or 1))
            for ing in ingredients(conn, r["id"]):
                req[ing["id"]] += ing["per_portion"] * batches
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

def refresh_shopping_list(conn, start: date | None = None, days: int | None = None,
                          reserve_days: int | None = None):
    """Gleicht die automatischen Einträge mit dem aktuellen Bedarf ab.

    Bestehende Einträge werden aktualisiert statt neu angelegt, damit die
    Verknüpfung zu Google Keep erhalten bleibt.
    """
    needs, hints = compute_needs(conn, start, days, reserve_days)
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


def create_product(conn, name, unit, location, pack_size=None):
    """Legt ein Produkt an, falls es den Namen noch nicht gibt; liefert in jedem Fall die id."""
    existing = find_product(conn, name)
    if existing:
        return existing["id"]
    if unit not in UNITS or location not in LOCATION_ORDER:
        raise ValueError("Einheit oder Lagerort ungültig.")
    cur = conn.execute(
        "INSERT INTO products(name, unit, location, pack_size) VALUES(?,?,?,?)",
        (name.strip(), unit, location, pack_size),
    )
    conn.commit()
    return cur.lastrowid


def update_product(conn, product_id, name, unit, location, category="", min_stock=0.0,
                   pack_size=None, price=None, active=True, factor=None):
    """Zutat ändern. Wechselt die Einheit, rechnen `factor` (1 neue Einheit = factor alte Einheiten)
    Bestand, Mindestbestand, Packungsgröße, Rezeptmengen und offene Einkaufsmengen mit um.

    Ohne `factor` bleiben die Zahlen, wie sie sind. Wirft ValueError bei ungültigen Angaben."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Der Name fehlt.")
    if unit not in UNITS or location not in LOCATION_ORDER:
        raise ValueError("Einheit oder Lagerort ungültig.")
    if factor is not None and factor <= 0:
        raise ValueError("Der Umrechnungsfaktor muss größer als 0 sein.")
    old = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not old:
        raise ValueError("Zutat nicht gefunden.")
    conv = factor if (factor and unit != old["unit"]) else None

    def c(x):
        return None if x is None else (x / conv if conv else x)

    try:
        conn.execute(
            "UPDATE products SET name = ?, unit = ?, location = ?, category = ?, min_stock = ?, "
            "pack_size = ?, price = ?, stock = ?, active = ? WHERE id = ?",
            (name, unit, location, (category or "").strip(), c(min_stock or 0.0), c(pack_size), price,
             c(old["stock"]), 1 if active else 0, product_id),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"Eine Zutat „{name}“ gibt es schon.")
    if conv:
        conn.execute("UPDATE recipe_ingredients SET qty = qty / ? WHERE product_id = ?", (conv, product_id))
        conn.execute(
            "UPDATE shopping_items SET qty = qty / ?, unit = ? WHERE product_id = ? AND status = 'open'",
            (conv, unit, product_id),
        )
    elif unit != old["unit"]:
        conn.execute("UPDATE shopping_items SET unit = ? WHERE product_id = ? AND status = 'open'", (unit, product_id))
    conn.commit()


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
