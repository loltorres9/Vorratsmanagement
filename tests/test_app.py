"""Tests – laufen mit `python -m pytest` oder `python -m unittest`."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from datetime import date, timedelta

from app import accounts, create_app, db, keep_sync, logic, recipe_csv, seed

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
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)")}
        self.assertTrue({"active", "servings"} <= cols)


class RecipeImportTest(unittest.TestCase):
    def test_import_uses_cookbook_amounts_for_one_portion(self):
        conn = fresh_conn()
        self.assertTrue(seed.apply_recipe_import(conn))
        self.assertFalse(seed.apply_recipe_import(conn))
        rid = conn.execute("SELECT id FROM recipes WHERE name='Lasagne'").fetchone()["id"]
        ings = {i["name"]: i["qty"] for i in logic.ingredients(conn, rid)}
        self.assertEqual(ings["Hackfleisch"], 500)
        self.assertEqual(ings["Lasagneplatten"], 200)
        self.assertEqual(ings["Milch"], 750)
        # nur, was im Rezept steht: Salat und Reibekäse stammen nicht aus dem Foto
        self.assertNotIn("Salat", ings)
        self.assertNotIn("Reibekäse", ings)
        self.assertNotIn("Passierte Tomaten", ings)
        per = {i["name"]: i["per_portion"] for i in logic.ingredients(conn, rid)}
        self.assertEqual(per["Hackfleisch"], 500)
        self.assertEqual(conn.execute("SELECT MAX(servings) FROM recipes").fetchone()[0], 1)

    def test_import_leaves_unstated_ingredients_out(self):
        conn = fresh_conn()
        seed.apply_recipe_import(conn)
        for name in ("Arme Ritter", "Spaghetti Bolognese", "Spaghetti mit Käsesoße", "Hähnchen-Reis-Pfanne"):
            rid = conn.execute("SELECT id FROM recipes WHERE name=?", (name,)).fetchone()["id"]
            names = {i["name"] for i in logic.ingredients(conn, rid)}
            self.assertFalse(names & {"Toastbrot", "Spaghetti", "Zitrone", "Paprika", "Erbsen & Möhren (TK)"}, name)

    def test_import_structure(self):
        conn = fresh_conn()
        seed.apply_recipe_import(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rotation").fetchone()[0], 35)
        for n in ("Wecknödel", "Serviettenknödel", "Spaghetti Carbonara"):
            self.assertIsNotNone(conn.execute("SELECT 1 FROM recipes WHERE name=?", (n,)).fetchone(), n)
        for r in conn.execute("SELECT id, name FROM recipes"):
            self.assertTrue(logic.ingredients(conn, r["id"]), r["name"])

    def test_gulasch_meals_back_to_seed_state(self):
        conn = fresh_conn()
        seed.apply_recipe_import(conn)
        rid = conn.execute("SELECT id FROM recipes WHERE name='Gulasch mit Wecknödel & Rotkraut'").fetchone()["id"]
        self.assertEqual({i["name"] for i in logic.ingredients(conn, rid)},
                         {"Gulasch-Portion", "Semmelknödel (Kochbeutel)", "Rotkraut (Glas)"})

    def test_import_from_previous_version_names(self):
        conn = fresh_conn()
        conn.execute("UPDATE recipes SET name='Hackfleischnudeln mit Paprika' WHERE name LIKE 'Hackfleischnudeln%'")
        conn.execute("INSERT INTO recipes(name, kind) VALUES('Spaghetti Carbonara (mit Schinken & Sahne)', 'meal')")
        conn.execute("UPDATE recipes SET servings = 4")
        conn.commit()
        seed.apply_recipe_import(conn)
        self.assertIsNotNone(conn.execute("SELECT 1 FROM recipes WHERE name='Hackfleischnudeln'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recipes WHERE name LIKE 'Spaghetti Carbonara%'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT MAX(servings) FROM recipes").fetchone()[0], 1)


class UserEditsProtectedTest(unittest.TestCase):
    def test_import_skips_recipes_edited_in_the_app(self):
        conn = fresh_conn()
        rid = conn.execute("SELECT id FROM recipes WHERE name='Lasagne mit Salat'").fetchone()["id"]
        reis = pid(conn, "Reis")
        logic.update_recipe(conn, rid, "Lasagne mit Salat", "meine Version", {reis: 77})
        seed.apply_recipe_import(conn)
        ings = {i["name"]: i["qty"] for i in logic.ingredients(conn, rid)}
        self.assertEqual(ings, {"Reis": 77})
        other = conn.execute("SELECT id FROM recipes WHERE name='Waffeln'").fetchone()["id"]
        self.assertIn("Mehl", {i["name"] for i in logic.ingredients(conn, other)})


class ProductEditTest(unittest.TestCase):
    def test_unit_change_converts_quantities(self):
        conn = fresh_conn()
        reis = pid(conn, "Reis")
        logic.set_stock(conn, reis, 1000)
        rid = conn.execute("SELECT recipe_id FROM recipe_ingredients WHERE product_id=? LIMIT 1", (reis,)).fetchone()
        before = conn.execute("SELECT qty FROM recipe_ingredients WHERE product_id=? AND recipe_id=?",
                              (reis, rid[0])).fetchone()[0]
        logic.update_product(conn, reis, "Reis", "Tasse", "Vorrat", "", 0, 1000, None, True, factor=200)
        row = conn.execute("SELECT * FROM products WHERE id=?", (reis,)).fetchone()
        self.assertEqual((row["unit"], row["stock"], row["pack_size"]), ("Tasse", 5, 5))
        after = conn.execute("SELECT qty FROM recipe_ingredients WHERE product_id=? AND recipe_id=?",
                             (reis, rid[0])).fetchone()[0]
        self.assertAlmostEqual(after, before / 200)

    def test_unit_change_without_factor_keeps_numbers(self):
        conn = fresh_conn()
        reis = pid(conn, "Reis")
        logic.set_stock(conn, reis, 3)
        logic.update_product(conn, reis, "Reis", "Tasse", "Vorrat", "", 0, 1000)
        self.assertEqual(stock(conn, "Reis"), 3)

    def test_validation(self):
        conn = fresh_conn()
        reis = pid(conn, "Reis")
        with self.assertRaises(ValueError):
            logic.update_product(conn, reis, "", "g", "Vorrat")
        with self.assertRaises(ValueError):
            logic.update_product(conn, reis, "Reis", "Liter", "Vorrat")
        with self.assertRaises(ValueError):
            logic.update_product(conn, reis, "Mehl", "g", "Vorrat")  # Name schon vergeben


class StartupKeepsDataTest(unittest.TestCase):
    def test_restart_does_not_touch_edited_recipes_or_run_import_twice(self):
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        cfg = {"TESTING": True, "DATABASE_PATH": path, "APP_PASSWORD": "x", "START_SYNC": False,
               "SESSION_COOKIE_SECURE": False}
        create_app(cfg)
        conn = db.connect(path)
        rid = conn.execute("SELECT id FROM recipes WHERE name='Lasagne'").fetchone()["id"]
        reis = pid(conn, "Reis")
        logic.update_recipe(conn, rid, "Lasagne", "meine Notiz", {reis: 42})
        conn.execute("DELETE FROM settings WHERE key LIKE 'import_%'")  # Merker fehlt, z. B. ältere Datenbank
        conn.commit()
        create_app(cfg)  # Neustart
        self.assertEqual([(i["name"], i["qty"]) for i in logic.ingredients(conn, rid)], [("Reis", 42)])
        self.assertEqual(conn.execute("SELECT notes FROM recipes WHERE id=?", (rid,)).fetchone()[0], "meine Notiz")


class StorageWarningTest(unittest.TestCase):
    def test_warns_on_railway_without_volume(self):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(), "RAILWAY_ENVIRONMENT": "production"}, clear=False):
            os.environ.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
            self.assertFalse(db.storage_is_persistent())
            tmp = os.environ["DATA_DIR"]
            with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": tmp}):
                self.assertTrue(db.storage_is_persistent())

    def test_no_warning_locally(self):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_ID"):
                os.environ.pop(k, None)
            self.assertTrue(db.storage_is_persistent())


class RailwayVolumePathTest(unittest.TestCase):
    def test_volume_path_wins_over_data_dir_variable(self):
        import unittest.mock as mock
        vol = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"DATA_DIR": "./data", "RAILWAY_VOLUME_MOUNT_PATH": vol,
                                          "RAILWAY_ENVIRONMENT": "production"}):
            self.assertEqual(str(db.data_dir()), vol)
            self.assertEqual(db.db_path().parent, Path(vol))
            self.assertTrue(db.storage_is_persistent())


class NewIngredientTest(unittest.TestCase):
    def test_create_product_is_idempotent_and_validates(self):
        conn = fresh_conn()
        a = logic.create_product(conn, "Lauch", "Stk", "Kühlschrank", 1)
        self.assertEqual(logic.create_product(conn, "lauch", "g", "Vorrat"), a)
        self.assertEqual(conn.execute("SELECT location FROM products WHERE id=?", (a,)).fetchone()[0], "Kühlschrank")
        with self.assertRaises(ValueError):
            logic.create_product(conn, "Komisch", "Liter", "Vorrat")


class PortionTest(unittest.TestCase):
    def test_cooking_and_needs_use_recipe_amounts(self):
        conn = fresh_conn()
        seed.apply_recipe_import(conn)
        rid = conn.execute("SELECT id FROM recipes WHERE name='Lasagne'").fetchone()["id"]
        hack = pid(conn, "Hackfleisch")
        logic.set_stock(conn, hack, 1000)
        logic.set_plan(conn, MONDAY.isoformat(), rid, 1)
        self.assertEqual(logic.requirements(conn, MONDAY, 1)[hack], 500)
        logic.cook_recipe(conn, rid, 1)
        self.assertEqual(stock(conn, "Hackfleisch"), 500)


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
        """Der bisherige Vorratsschrank gehört dem, der das alte App-Passwort nennt."""
        r = self.c.post("/registrieren", data={"name": "ich", "password": "langespasswort",
                                               "mode": "new", "claim_password": "geheim"})
        self.assertEqual(r.status_code, 302)

    def test_requires_login(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.post("/login", data={"name": "x", "password": "falsch"}).status_code, 200)

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

    def test_ingredients_tab(self):
        self.login()
        self.assertEqual(self.c.get("/zutaten").status_code, 200)
        conn = db.connect(self.app.config["DATABASE_PATH"])
        reis = pid(conn, "Reis")
        self.assertEqual(self.c.get(f"/zutaten/{reis}").status_code, 200)
        r = self.c.post(f"/zutaten/{reis}", data={
            "name": "Reis", "unit": "Tasse", "location": "Vorrat", "category": "", "min_stock": "0",
            "pack_size": "5", "price": "", "active": "1", "factor": "200"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Tasse", self.c.get("/zutaten").get_data(as_text=True))
        self.assertEqual(self.c.get("/rezepte").status_code, 200)

    def test_recipe_edit_creates_new_ingredient(self):
        self.login()
        conn = db.connect(self.app.config["DATABASE_PATH"])
        rid = conn.execute("SELECT id FROM recipes WHERE kind='meal' ORDER BY id").fetchone()["id"]
        data = {f"qty_{i['id']}": str(i["qty"]) for i in logic.ingredients(conn, rid)}
        data.update({"name": "Test", "new_name": "Frischer Basilikum", "new_unit": "Stk",
                     "new_location": "Kühlschrank", "new_qty": "1"})
        self.assertEqual(self.c.post(f"/rezepte/{rid}/edit", data=data).status_code, 302)
        names = {i["name"] for i in logic.ingredients(conn, rid)}
        self.assertIn("Frischer Basilikum", names)

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


class MultiUserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = {"TESTING": True, "DATABASE_PATH": os.path.join(self.tmp, "w.db"),
                    "APP_PASSWORD": "geheim", "START_SYNC": False, "SESSION_COOKIE_SECURE": False}
        self.app = create_app(self.cfg)

    def client(self, name, **extra):
        c = self.app.test_client()
        data = {"name": name, "password": "langespasswort", "mode": "new"}
        data.update(extra)
        self.assertEqual(c.post("/registrieren", data=data).status_code, 302)
        return c

    def household_conn(self, hid):
        return db.connect(os.path.join(self.tmp, "households", f"{hid}.db"))

    def test_claiming_old_cabinet_needs_old_password(self):
        c = self.app.test_client()
        r = c.post("/registrieren", data={"name": "dieb", "password": "langespasswort", "claim_password": "falsch"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("stimmt nicht", r.get_data(as_text=True))
        self.client("ich", claim_password="geheim")
        aconn = accounts.connect(os.path.join(self.tmp, "accounts.db"))
        self.assertTrue(accounts.is_claimed(aconn))
        self.assertEqual(accounts.get_user(aconn, 1)["household_id"], accounts.LEGACY_ID)

    def test_new_user_gets_own_empty_cabinet(self):
        self.client("ich", claim_password="geheim")
        neu = self.client("neu")
        conn = self.household_conn(2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        neu.post("/einkauf/add", data={"name": "Kaffee", "qty": ""})
        self.assertIn("Kaffee", neu.get("/einkauf").get_data(as_text=True))
        legacy = db.connect(self.cfg["DATABASE_PATH"])
        self.assertEqual(legacy.execute("SELECT COUNT(*) FROM shopping_items WHERE name='Kaffee'").fetchone()[0], 0)

    def test_sample_data_is_optional(self):
        self.client("ich", claim_password="geheim")
        self.client("mit", sample="1")
        self.assertEqual(self.household_conn(2).execute(
            "SELECT COUNT(*) FROM recipes WHERE kind='meal'").fetchone()[0] > 30, True)

    def test_join_with_code_shares_data(self):
        a = self.client("anna", claim_password="geheim")
        code = accounts.show_code(accounts.get_household(
            accounts.connect(os.path.join(self.tmp, "accounts.db")), 1)["join_code"])
        self.assertIn(code, a.get("/haushalt").get_data(as_text=True))
        b = self.app.test_client()
        bad = b.post("/registrieren", data={"name": "ben", "password": "langespasswort", "mode": "join",
                                            "join_code": "NOPE-NOPE"})
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(b.post("/registrieren", data={"name": "ben", "password": "langespasswort", "mode": "join",
                                                       "join_code": code.lower()}).status_code, 302)
        a.post("/einkauf/add", data={"name": "Kaffee", "qty": ""})
        self.assertIn("Kaffee", b.get("/einkauf").get_data(as_text=True))
        aconn = accounts.connect(os.path.join(self.tmp, "accounts.db"))
        self.assertEqual([m["role"] for m in accounts.members(aconn, 1)], ["owner", "member"])

    def test_only_owner_changes_code_and_old_code_dies(self):
        a = self.client("anna", claim_password="geheim")
        aconn = accounts.connect(os.path.join(self.tmp, "accounts.db"))
        old = accounts.get_household(aconn, 1)["join_code"]
        b = self.app.test_client()
        b.post("/registrieren", data={"name": "ben", "password": "langespasswort", "mode": "join", "join_code": old})
        b.post("/haushalt/code")
        self.assertEqual(accounts.get_household(aconn, 1)["join_code"], old)
        a.post("/haushalt/code")
        self.assertNotEqual(accounts.get_household(aconn, 1)["join_code"], old)
        self.assertIsNone(accounts.find_by_code(aconn, old))

    def test_login_and_logout(self):
        self.client("anna", claim_password="geheim")
        c = self.app.test_client()
        self.assertEqual(c.post("/login", data={"name": "ANNA", "password": "falsch1234"}).status_code, 200)
        self.assertEqual(c.post("/login", data={"name": "ANNA", "password": "langespasswort"}).status_code, 302)
        self.assertEqual(c.get("/").status_code, 200)
        c.post("/logout")
        self.assertEqual(c.get("/").status_code, 302)

    def test_duplicate_name_and_short_password_rejected(self):
        self.client("anna", claim_password="geheim")
        c = self.app.test_client()
        for data in ({"name": "Anna", "password": "langespasswort"}, {"name": "neu", "password": "kurz"}):
            self.assertEqual(c.post("/registrieren", data=data).status_code, 200)

    def test_last_member_joining_elsewhere_deletes_old_cabinet(self):
        self.client("anna", claim_password="geheim")
        ben = self.client("ben")
        path = os.path.join(self.tmp, "households", "2.db")
        self.assertTrue(os.path.exists(path))
        aconn = accounts.connect(os.path.join(self.tmp, "accounts.db"))
        code = accounts.get_household(aconn, 1)["join_code"]
        ben.post("/haushalt/beitreten", data={"join_code": code})  # ohne Bestätigung: nichts passiert
        self.assertEqual(accounts.get_user(aconn, 2)["household_id"], 2)
        ben.post("/haushalt/beitreten", data={"join_code": code, "confirm": "1"})
        self.assertEqual(accounts.get_user(aconn, 2)["household_id"], 1)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(accounts.get_household(aconn, 2))

    def test_leaving_makes_own_cabinet_and_promotes_member(self):
        a = self.client("anna", claim_password="geheim")
        aconn = accounts.connect(os.path.join(self.tmp, "accounts.db"))
        b = self.app.test_client()
        b.post("/registrieren", data={"name": "ben", "password": "langespasswort", "mode": "join",
                                      "join_code": accounts.get_household(aconn, 1)["join_code"]})
        a.post("/haushalt/verlassen")
        self.assertEqual(accounts.get_user(aconn, 1)["household_id"], 2)
        self.assertEqual(accounts.get_user(aconn, 2)["role"], "owner")  # Ben ist nun Besitzer des alten Schranks
        self.assertEqual(accounts.get_user(aconn, 1)["role"], "owner")
        self.assertEqual(self.household_conn(2).execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)

    def test_restart_keeps_all_cabinets(self):
        self.client("anna", claim_password="geheim")
        self.client("ben")
        create_app(self.cfg)  # Neustart
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "households", "2.db")))


class RecipeCsvTest(unittest.TestCase):
    def conn(self):
        c = db.connect(os.path.join(tempfile.mkdtemp(), "c.db"))
        db.init(c)
        return c

    def test_template_roundtrip(self):
        recipes, errors = recipe_csv.parse(recipe_csv.decode(recipe_csv.template_csv().encode("utf-8")))
        self.assertEqual(errors, [])
        self.assertEqual([r["name"] for r in recipes], ["Spaghetti Bolognese", "Gemüsepfanne mit Reis"])
        self.assertEqual(len(recipes[0]["ingredients"]), 4)
        conn = self.conn()
        res = recipe_csv.apply(conn, recipes)
        self.assertEqual(len(res["created"]), 2)
        rid = conn.execute("SELECT id FROM recipes WHERE name='Spaghetti Bolognese'").fetchone()["id"]
        ings = {i["name"]: i for i in logic.ingredients(conn, rid)}
        self.assertEqual(ings["Rinderhack"]["qty"], 150)
        self.assertEqual(ings["Rinderhack"]["location"], "Kühlschrank")
        self.assertEqual(ings["Rinderhack"]["pack_size"], 500)
        self.assertEqual(ings["Rinderhack"]["category"], "Fleisch")
        self.assertIn("köcheln", conn.execute("SELECT notes FROM recipes WHERE id=?", (rid,)).fetchone()["notes"])

    def test_instructions_name_all_columns_units_and_locations(self):
        text = recipe_csv.ai_instructions()
        for token in recipe_csv.COLUMNS + list(logic.UNITS) + list(logic.LOCATIONS):
            self.assertIn(token, text)

    def test_comma_delimiter_decimal_comma_and_unit_conversion(self):
        text = 'Rezept,Zutat,Menge,Einheit\nSuppe,Wasser,"0,5",l\nSuppe,Salzkartoffeln,0.2,kg\n'
        recipes, errors = recipe_csv.parse(text)
        self.assertEqual(errors, [])
        ings = {i["name"]: (i["qty"], i["unit"]) for i in recipes[0]["ingredients"]}
        self.assertEqual(ings, {"Wasser": (500, "ml"), "Salzkartoffeln": (200, "g")})

    def test_same_ingredient_twice_is_summed(self):
        recipes, _ = recipe_csv.parse("rezept;zutat;menge;einheit\nA;Mehl;100;g\nA;mehl;50;g\n")
        self.assertEqual([(i["qty"]) for i in recipes[0]["ingredients"]], [150])

    def test_bad_recipe_is_skipped_others_import(self):
        text = ("rezept;zutat;menge;einheit\nGut;Reis;80;g\nSchlecht;Reis;viel;g\nSchlecht;Ei;2;Stk\n"
                "Auch schlecht;Ei;2;Eimer\n")
        recipes, errors = recipe_csv.parse(text)
        self.assertEqual([r["name"] for r in recipes], ["Gut"])
        self.assertEqual(len(errors), 4)
        self.assertTrue(any("Zeile 3" in e for e in errors))

    def test_missing_column_and_empty_file(self):
        for text in ("rezept;zutat\nA;B\n", "  \n"):
            with self.assertRaises(recipe_csv.CsvError):
                recipe_csv.parse(text)

    def test_unit_mismatch_with_known_product_skips_recipe_without_side_effects(self):
        conn = self.conn()
        logic.create_product(conn, "Reis", "g", "Vorrat")
        recipes, _ = recipe_csv.parse("rezept;zutat;menge;einheit\nPfanne;Paprika;1;Stk\nPfanne;Reis;2;Tasse\n")
        res = recipe_csv.apply(conn, recipes)
        self.assertEqual(res["created"], [])
        self.assertTrue(res["errors"])
        self.assertIsNone(logic.find_product(conn, "Paprika"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0], 0)

    def test_existing_recipe_skipped_or_overwritten(self):
        conn = self.conn()
        first, _ = recipe_csv.parse("rezept;zutat;menge;einheit\nA;Reis;80;g\n")
        recipe_csv.apply(conn, first)
        second, _ = recipe_csv.parse("rezept;zutat;menge;einheit\na;Reis;90;g\n")
        self.assertEqual(recipe_csv.apply(conn, second)["skipped"], ["a"])
        self.assertEqual(recipe_csv.apply(conn, second, overwrite=True)["updated"], ["a"])
        rid = conn.execute("SELECT id FROM recipes").fetchone()["id"]
        self.assertEqual(logic.ingredients(conn, rid)[0]["qty"], 90)

    def test_encoding_fallback_for_excel_files(self):
        raw = "rezept;zutat;menge;einheit\nKäsespätzle;Käse;100;g\n".encode("cp1252")
        recipes, _ = recipe_csv.parse(recipe_csv.decode(raw))
        self.assertEqual(recipes[0]["name"], "Käsespätzle")


class RecipeCsvWebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.app = create_app({"TESTING": True, "DATABASE_PATH": os.path.join(tmp, "w.db"), "START_SYNC": False,
                               "APP_PASSWORD": "geheim", "SESSION_COOKIE_SECURE": False})
        self.c = self.app.test_client()
        self.c.post("/registrieren", data={"name": "ich", "password": "langespasswort", "claim_password": "geheim"})

    def test_downloads_and_upload(self):
        import io
        page = self.c.get("/rezepte/import")
        self.assertEqual(page.status_code, 200)
        tpl = self.c.get("/rezepte/vorlage.csv")
        self.assertIn("text/csv", tpl.content_type)
        self.assertIn("attachment", tpl.headers["Content-Disposition"])
        self.assertEqual(self.c.get("/rezepte/ki-anleitung.txt").status_code, 200)
        r = self.c.post("/rezepte/import", data={"datei": (io.BytesIO(
            b"rezept;zutat;menge;einheit\nTestgericht Neu;Reis;80;g\n"), "x.csv")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Testgericht Neu", r.get_data(as_text=True))
        self.assertIn("Testgericht Neu", self.c.get("/rezepte").get_data(as_text=True))
        bad = self.c.post("/rezepte/import", data={"datei": (io.BytesIO(b"a;b\n1;2\n"), "x.csv")},
                          content_type="multipart/form-data")
        self.assertIn("fehlen die Spalten", bad.get_data(as_text=True))

    def test_import_requires_login(self):
        self.assertEqual(self.app.test_client().get("/rezepte/import").status_code, 302)


class RecipeCsvActiveAndReplaceTest(unittest.TestCase):
    def test_active_column(self):
        recipes, errors = recipe_csv.parse(
            "rezept;zutat;menge;einheit;aktiv\nA;Reis;80;g;nein\nA;Ei;1;Stk;\nB;Reis;80;g;\nC;Reis;80;g;ja\n")
        self.assertEqual(errors, [])
        self.assertEqual({r["name"]: r["active"] for r in recipes}, {"A": False, "B": True, "C": True})
        _, errors = recipe_csv.parse("rezept;zutat;menge;einheit;aktiv\nA;Reis;80;g;vielleicht\n")
        self.assertTrue(errors)
        conn = fresh_conn()
        recipe_csv.apply(conn, recipes)
        act = {r["name"]: r["active"] for r in conn.execute("SELECT name, active FROM recipes WHERE name IN ('A','B','C')")}
        self.assertEqual(act, {"A": 0, "B": 1, "C": 1})

    def test_delete_all_keeps_batch_behind_made_product(self):
        conn = fresh_conn()
        logic.ensure_plan(conn, MONDAY, 14)
        kept = {r[0] for r in conn.execute("SELECT made_by_recipe_id FROM products WHERE made_by_recipe_id IS NOT NULL")}
        self.assertTrue(kept)
        n = logic.delete_all_recipes(conn)
        self.assertGreater(n, 30)
        self.assertEqual({r["id"] for r in conn.execute("SELECT id FROM recipes")}, kept)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rotation").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meal_plan").fetchone()[0], 0)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 10)


class RecipeCsvReplaceWebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.app = create_app({"TESTING": True, "DATABASE_PATH": os.path.join(tmp, "w.db"), "START_SYNC": False,
                               "APP_PASSWORD": "geheim", "SESSION_COOKIE_SECURE": False})
        self.c = self.app.test_client()
        self.c.post("/registrieren", data={"name": "ich", "password": "langespasswort", "claim_password": "geheim"})
        self.db = db.connect(self.app.config["DATABASE_PATH"])

    def upload(self, text, **form):
        import io
        data = {"datei": (io.BytesIO(text.encode("utf-8")), "x.csv"), **form}
        return self.c.post("/rezepte/import", data=data, content_type="multipart/form-data")

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM recipes WHERE kind='meal'").fetchone()[0]

    def test_replace_needs_confirmation_and_valid_file(self):
        before = self.count()
        good = "rezept;zutat;menge;einheit\nNeu;Reis;80;g\n"
        self.upload(good, replace_all="1")  # ohne Bestätigung
        self.assertEqual(self.count(), before)
        self.upload("rezept;zutat;menge;einheit\nKaputt;Reis;viel;g\n", replace_all="1", confirm="LÖSCHEN")
        self.assertEqual(self.count(), before)  # fehlerhafte Datei löscht nichts
        self.upload("rezept;zutat;menge;einheit\nUnpassend;Reis;1;Stk\n", replace_all="1", confirm="LÖSCHEN")
        self.assertEqual(self.count(), before)  # Einheit passt nicht zu vorhandener Zutat
        r = self.upload(good, replace_all="1", confirm="löschen")
        self.assertIn("gelöscht", r.get_data(as_text=True))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.db.execute("SELECT name FROM recipes WHERE kind='meal'").fetchone()["name"], "Neu")


if __name__ == "__main__":
    unittest.main()
