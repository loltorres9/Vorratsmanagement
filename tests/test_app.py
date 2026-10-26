"""Tests – laufen mit `python -m pytest` oder `python -m unittest`."""
import os
import tempfile
import unittest
import uuid
from datetime import date, timedelta

from app import create_app, db, keep_sync, logic, seed

MONDAY = date(2026, 11, 2)


def fresh_conn():
    tmp = tempfile.mkdtemp()
    conn = db.connect(os.path.join(tmp, "t.db"))
    db.init(conn)
    seed.load(conn)
    db.set_setting(conn, "rotation_start", MONDAY.isoformat())
    conn.commit()
    return conn


def pid(conn, name):
    return conn.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()["id"]


def stock(conn, name):
    return conn.execute("SELECT stock FROM products WHERE name = ?", (name,)).fetchone()["stock"]


class SeedTest(unittest.TestCase):
    def test_seed_complete(self):
        conn = fresh_conn()
        n_meal = conn.execute("SELECT COUNT(*) FROM recipes WHERE kind='meal'").fetchone()[0]
        n_rot = conn.execute("SELECT COUNT(*) FROM rotation").fetchone()[0]
        self.assertEqual(n_meal, 35)
        self.assertEqual(n_rot, 35)
        for r in conn.execute("SELECT id FROM recipes"):
            self.assertTrue(logic.ingredients(conn, r["id"]), "Rezept ohne Zutaten")


class PlanTest(unittest.TestCase):
    def test_rotation_wraps(self):
        conn = fresh_conn()
        r1, w1 = logic.rotation_recipe(conn, MONDAY)
        r6, w6 = logic.rotation_recipe(conn, MONDAY + timedelta(weeks=5))
        self.assertEqual((w1, w6), (1, 1))
        self.assertEqual(r1, r6)

    def test_ensure_plan_keeps_manual_changes(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 7)
        other = conn.execute("SELECT id FROM recipes WHERE name='Apfelküchle'").fetchone()["id"]
        logic.set_plan(conn, MONDAY.isoformat(), other)
        logic.ensure_plan(conn, MONDAY, 7)
        row = conn.execute("SELECT recipe_id FROM meal_plan WHERE date=?", (MONDAY.isoformat(),)).fetchone()
        self.assertEqual(row["recipe_id"], other)


class RecipeEditTest(unittest.TestCase):
    def test_inactive_recipe_leaves_rotation_and_plan(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 7)
        rid, _ = logic.rotation_recipe(conn, MONDAY)
        old_today = logic.today
        logic.today = lambda: MONDAY
        try:
            logic.set_recipe_active(conn, rid, False)
        finally:
            logic.today = old_today
        self.assertIsNone(logic.rotation_recipe(conn, MONDAY)[0])
        self.assertIsNone(conn.execute("SELECT 1 FROM meal_plan WHERE date=?", (MONDAY.isoformat(),)).fetchone())
        logic.ensure_plan(conn, MONDAY, 7)
        self.assertIsNone(conn.execute("SELECT 1 FROM meal_plan WHERE date=?", (MONDAY.isoformat(),)).fetchone())
        logic.set_recipe_active(conn, rid, True)
        self.assertEqual(logic.rotation_recipe(conn, MONDAY)[0], rid)

    def test_update_recipe(self):
        conn = fresh_conn()
        rid, _ = logic.rotation_recipe(conn, MONDAY)
        reis = pid(conn, "Reis")
        logic.update_recipe(conn, rid, " Neues Gericht ", "Notiz", {reis: 123})
        r = conn.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
        self.assertEqual((r["name"], r["notes"]), ("Neues Gericht", "Notiz"))
        self.assertEqual([(i["name"], i["qty"]) for i in logic.ingredients(conn, rid)], [("Reis", 123)])
        with self.assertRaises(ValueError):
            logic.update_recipe(conn, rid, "", "", {reis: 1})
        with self.assertRaises(ValueError):
            logic.update_recipe(conn, rid, "X", "", {reis: 0})

    def test_migration_adds_active_column(self):
        tmp = tempfile.mkdtemp()
        conn = db.connect(os.path.join(tmp, "old.db"))
        conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, "
                     "yields_product_id INTEGER, yields_qty REAL, notes TEXT)")
        db.migrate(conn)
        self.assertIn("active", {r["name"] for r in conn.execute("PRAGMA table_info(recipes)")})


class RecipeImportTest(unittest.TestCase):
    def test_import_applies_once(self):
        conn = fresh_conn()
        self.assertTrue(seed.apply_recipe_import(conn))
        self.assertFalse(seed.apply_recipe_import(conn))
        lasagne = conn.execute("SELECT id FROM recipes WHERE name='Lasagne mit Salat'").fetchone()["id"]
        ings = {i["name"]: i["qty"] for i in logic.ingredients(conn, lasagne)}
        self.assertEqual(ings["Hackfleisch"], 125)
        self.assertEqual(ings["Lasagneplatten"], 50)
        self.assertIn("Salat", ings)
        self.assertNotIn("Passierte Tomaten", ings)
        self.assertIsNotNone(conn.execute("SELECT 1 FROM recipes WHERE name='Spaghetti Bolognese'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM recipes WHERE name LIKE 'Spaghetti Carbonara%'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rotation").fetchone()[0], 35)
        # alle Rotationsgerichte haben weiterhin Zutaten
        for r in conn.execute("SELECT id FROM recipes"):
            self.assertTrue(logic.ingredients(conn, r["id"]))


class NeedsTest(unittest.TestCase):
    def test_week_needs_and_batch_expansion(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 7)
        needs, hints = logic.compute_needs(conn, MONDAY, 7)
        names = {n["name"]: n for n in needs}
        # Woche 1 Samstag = Gulasch -> Batch wird aufgelöst
        self.assertIn("Gulaschfleisch", names)
        self.assertNotIn("Gulasch-Portion", names)
        self.assertTrue(any("Gulasch" in h for h in hints))
        # Aufrunden auf Packungen
        self.assertEqual(names["Gulaschfleisch"]["qty"], 1000)
        self.assertEqual(names["Hähnchenbrust"]["packs"], 1)

    def test_stock_reduces_needs(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 7)
        logic.set_stock(conn, pid(conn, "Gulasch-Portion"), 4)
        needs, hints = logic.compute_needs(conn, MONDAY, 7)
        self.assertNotIn("Gulaschfleisch", {n["name"] for n in needs})
        self.assertEqual(hints, [])

    def test_reserve_only_for_durable_products(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 14)
        short, _ = logic.compute_needs(conn, MONDAY, 7, reserve_days=7)
        long, _ = logic.compute_needs(conn, MONDAY, 7, reserve_days=10)
        short = {n["name"]: n for n in short}
        long = {n["name"]: n for n in long}
        self.assertTrue(set(short) <= set(long))
        for name, n in long.items():
            if n["location"] == "Kühlschrank":
                self.assertEqual(n["need"], short[name]["need"], name)
            else:
                self.assertGreaterEqual(n["need"], short.get(name, {"need": 0})["need"] - 1e-9, name)
        self.assertTrue(any(n["location"] != "Kühlschrank" and n["need"] > short.get(k, {"need": 0})["need"]
                            for k, n in long.items()))

    def test_freezer_capacity_shortens_tk_reserve(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 14)
        pommes = pid(conn, "Pommes (TK)")
        # Pommes an jedem Tag der Rotation: viel TK-Bedarf
        conn.execute("UPDATE recipe_ingredients SET qty = 400 WHERE product_id = ?", (pommes,))
        conn.commit()
        free, _ = logic.compute_needs(conn, MONDAY, 7, reserve_days=10, tk_capacity_g=10**9)
        capped, hints = logic.compute_needs(conn, MONDAY, 7, reserve_days=10, tk_capacity_g=2500)

        def tk(ns):
            return sum(n["qty"] for n in ns if n["location"] == "TK")
        self.assertLessEqual(tk(capped), tk(free))
        if tk(capped) < tk(free):
            self.assertTrue(any("Tiefkühlfach" in h for h in hints))
        self.assertFalse(any("Tiefkühlfach" in h for h in logic.compute_needs(
            conn, MONDAY, 7, reserve_days=10, tk_capacity_g=10**9)[1]))

    def test_min_stock_without_plan(self):
        conn = fresh_conn()
        needs, _ = logic.compute_needs(conn, MONDAY, 0)
        names = {n["name"] for n in needs}
        self.assertIn("Mehl", names)       # min 500
        self.assertNotIn("Grieß", names)   # min 0, kein Bedarf


class ShoppingTest(unittest.TestCase):
    def test_refresh_is_idempotent_and_buy_updates_stock(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 7)
        logic.refresh_shopping_list(conn, MONDAY, 7)
        first = {r["id"] for r in logic.shopping_list(conn)}
        logic.refresh_shopping_list(conn, MONDAY, 7)
        self.assertEqual(first, {r["id"] for r in logic.shopping_list(conn)})

        item = next(r for r in logic.shopping_list(conn) if r["name"] == "Mehl")
        logic.mark_bought(conn, item["id"])
        self.assertEqual(stock(conn, "Mehl"), item["qty"])

    def test_cook_deducts_and_batch_produces(self):
        conn = fresh_conn()
        logic.set_stock(conn, pid(conn, "Gulaschfleisch"), 1000)
        batch = conn.execute("SELECT id FROM recipes WHERE kind='batch'").fetchone()["id"]
        logic.cook_recipe(conn, batch)
        self.assertEqual(stock(conn, "Gulasch-Portion"), 4)
        self.assertEqual(stock(conn, "Gulaschfleisch"), 200)

    def test_manual_item_blocks_auto_duplicate(self):
        conn = fresh_conn()
        logic.add_manual(conn, "mehl", 2000)
        logic.refresh_shopping_list(conn, MONDAY, 0)
        rows = [r for r in logic.shopping_list(conn) if r["name"] == "Mehl"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "manual")


# ------------------------------------------------------------- Keep-Fake

class FakeItem:
    def __init__(self, text, checked=False):
        self.id = uuid.uuid4().hex
        self.text = text
        self.checked = checked
        self.deleted = False

    def delete(self):
        self.deleted = True


class FakeList:
    def __init__(self):
        self._items = []

    @property
    def items(self):
        return [i for i in self._items if not i.deleted]

    def add(self, text, checked=False):
        it = FakeItem(text, checked)
        self._items.append(it)
        return it


class KeepSyncTest(unittest.TestCase):
    def test_two_way(self):
        conn = fresh_conn()
        logic.refresh_shopping_list(conn, MONDAY, 0)  # nur Mindestbestände
        gl = FakeList()
        s = keep_sync.sync_list(conn, gl)
        n_open = len(logic.shopping_list(conn))
        self.assertEqual(s["added"], n_open)
        self.assertEqual(len(gl.items), n_open)

        # in Keep abhaken -> gekauft + Bestand
        mehl = next(i for i in gl.items if i.text.startswith("Mehl"))
        mehl.checked = True
        # in Keep neu hinzufügen -> übernommen
        gl.add("Kaffee")
        # in Keep löschen -> verworfen
        eier = next(i for i in gl.items if i.text.startswith("Eier"))
        eier.delete()

        s = keep_sync.sync_list(conn, gl)
        self.assertEqual((s["bought"], s["imported"], s["dismissed"]), (1, 1, 1))
        self.assertEqual(stock(conn, "Mehl"), 1000)
        self.assertTrue(any(r["name"] == "Kaffee" for r in logic.shopping_list(conn)))
        self.assertFalse(any(i.text.startswith("Mehl") for i in gl.items))

        # zweiter Lauf ohne Änderungen ändert nichts
        s = keep_sync.sync_list(conn, gl)
        self.assertEqual(sum(s.values()), 0)

    def test_app_side_purchase_removes_from_keep(self):
        conn = fresh_conn()
        logic.refresh_shopping_list(conn, MONDAY, 0)
        gl = FakeList()
        keep_sync.sync_list(conn, gl)
        item = next(r for r in logic.shopping_list(conn) if r["name"] == "Milch")
        logic.mark_bought(conn, item["id"])
        s = keep_sync.sync_list(conn, gl)
        self.assertEqual(s["removed"], 1)
        self.assertFalse(any(i.text.startswith("Milch") for i in gl.items))


# ------------------------------------------------------------- Web

class WebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.app = create_app({
            "TESTING": True, "DATABASE_PATH": os.path.join(tmp, "w.db"),
            "APP_PASSWORD": "geheim", "START_SYNC": False, "SESSION_COOKIE_SECURE": False,
        })
        self.c = self.app.test_client()

    def login(self):
        r = self.c.post("/login", data={"password": "geheim"})
        self.assertEqual(r.status_code, 302)

    def test_requires_login(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.post("/login", data={"password": "falsch"}).status_code, 200)

    def test_pages_render(self):
        self.login()
        for path in ["/", "/plan", "/einkauf", "/vorrat", "/rezepte", "/healthz"]:
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_flow(self):
        self.login()
        self.assertEqual(self.c.post("/einkauf/refresh").status_code, 302)
        page = self.c.get("/einkauf").get_data(as_text=True)
        self.assertIn("Mehl", page)
        self.c.post("/einkauf/add", data={"name": "Kaffee", "qty": ""})
        self.assertIn("Kaffee", self.c.get("/einkauf").get_data(as_text=True))
        t = logic.today().isoformat()
        self.assertEqual(self.c.post(f"/plan/{t}/cook").status_code, 302)

    def test_recipe_edit_and_deactivate(self):
        self.login()
        conn = db.connect(self.app.config["DATABASE_PATH"])
        rid = conn.execute("SELECT id FROM recipes WHERE kind='meal' ORDER BY id").fetchone()["id"]
        page = self.c.get(f"/rezepte/{rid}/edit")
        self.assertEqual(page.status_code, 200)
        ings = logic.ingredients(conn, rid)
        data = {"name": "Umbenannt", "notes": ""}
        data.update({f"qty_{i['id']}": "55" for i in ings})
        self.assertEqual(self.c.post(f"/rezepte/{rid}/edit", data=data).status_code, 302)
        self.assertIn("Umbenannt", self.c.get("/rezepte").get_data(as_text=True))
        self.assertEqual(self.c.post(f"/rezepte/{rid}/active", data={"active": "0"}).status_code, 302)
        self.assertIn("inaktiv", self.c.get("/rezepte").get_data(as_text=True))
        self.assertEqual(self.c.get("/plan").status_code, 200)


if __name__ == "__main__":
    unittest.main()
