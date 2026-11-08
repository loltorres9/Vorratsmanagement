"""Vorrat – Inventar, Speiseplan und Einkaufsliste für einen Haushalt."""
import hmac
import logging
import os
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)

from . import accounts, db, keep_sync, logic, recipe_csv, seed

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def create_app(test_config=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        APP_PASSWORD=os.environ.get("APP_PASSWORD", ""),
        DATABASE_PATH=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),
        START_SYNC=True,
        ACCOUNTS_PATH=None,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    if not app.config["APP_PASSWORD"]:
        app.logger.warning("APP_PASSWORD ist nicht gesetzt – der erste Benutzer übernimmt den bestehenden "
                           "Vorratsschrank ohne Nachweis (nur lokal verwenden!)")

    def legacy_db_path():
        return app.config["DATABASE_PATH"] or db.db_path()

    def make_conn():
        """Datenbank des bisherigen Schranks (Haushalt 1); der Keep-Sync läuft nur dort."""
        return db.connect(legacy_db_path())

    def household_path(hid):
        if hid == accounts.LEGACY_ID:
            return legacy_db_path()
        d = Path(legacy_db_path()).parent / "households"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{hid}.db"

    def accounts_path():
        return app.config["ACCOUNTS_PATH"] or Path(legacy_db_path()).parent / "accounts.db"

    def make_aconn():
        return accounts.connect(accounts_path())

    def prepare_household(hid, sample=False):
        """Schema anlegen/nachrüsten; der bisherige Schrank bekommt wie früher Startdaten und Rezeptimport,
        ein neuer nur auf Wunsch die Beispieldaten."""
        conn = db.connect(household_path(hid))
        db.init(conn)
        if hid == accounts.LEGACY_ID:
            if seed.ensure_seeded(conn):
                app.logger.warning("Leere Datenbank: Startdaten geladen (neue Datenbank, prüfe das Volume unter /data)")
            # Einmalig (Merker in settings); in der App bearbeitete Rezepte bleiben unberührt.
            if seed.apply_recipe_import(conn):
                app.logger.info("Rezepte aus dem Rezeptordner übernommen")
        elif sample and seed.is_empty(conn):
            seed.load(conn)
            seed.apply_recipe_import(conn)
        logic.ensure_plan(conn)
        conn.close()

    def drop_household(hid):
        """Löscht einen leeren Schrank samt Datenbankdatei (nie den bisherigen Schrank)."""
        if hid == accounts.LEGACY_ID:
            return
        aconn = make_aconn()
        accounts.delete_household(aconn, hid)
        aconn.close()
        base = household_path(hid)
        for suffix in ("", "-wal", "-shm"):
            Path(str(base) + suffix).unlink(missing_ok=True)

    if not db.storage_is_persistent():
        app.logger.error(
            "DATA_DIR=%s ist kein Volume: Die Datenbank geht bei jedem Deploy verloren. "
            "In Railway ein Volume an diesen Pfad hängen. Details: %s", db.data_dir(), db.storage_info())

    with app.app_context():
        aconn = make_aconn()
        accounts.init(aconn)
        accounts.ensure_legacy_household(aconn)
        hids = [h["id"] for h in accounts.all_households(aconn)]
        aconn.close()
        for hid in hids:
            prepare_household(hid)

    if app.config["START_SYNC"]:
        keep_sync.start_background(make_conn)

    # ------------------------------------------------------------- Infrastruktur

    def get_aconn():
        if "aconn" not in g:
            g.aconn = make_aconn()
        return g.aconn

    def get_conn():
        """Datenbank des Vorratsschranks des angemeldeten Benutzers."""
        if "conn" not in g:
            g.conn = db.connect(household_path(g.household["id"]))
        return g.conn

    @app.teardown_appcontext
    def close_conns(exc):
        for key in ("conn", "aconn"):
            c = g.pop(key, None)
            if c is not None:
                c.close()

    @app.before_request
    def load_user():
        g.user = g.household = None
        uid = session.get("uid")
        if uid:
            user = accounts.get_user(get_aconn(), uid)
            if user:
                g.user = user
                g.household = accounts.get_household(get_aconn(), user["household_id"])
            else:
                session.clear()

    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapped

    def owner_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if g.user["role"] != "owner":
                flash("Das darf nur der Besitzer des Vorratsschranks.")
                return redirect(url_for("household_view"))
            return view(*a, **kw)
        return login_required(wrapped)

    def is_keep_household():
        return g.household is not None and g.household["id"] == accounts.LEGACY_ID

    @app.context_processor
    def helpers():
        def day_label(iso):
            d = date.fromisoformat(iso)
            t = logic.today()
            if d == t:
                return "Heute"
            if d == t + timedelta(days=1):
                return "Morgen"
            return f"{WEEKDAYS[d.weekday()]} {d.day}.{d.month}."
        def num(x):
            return logic.fmt_qty(x, "").strip()

        return {
            "fmt_qty": logic.fmt_qty,
            "num": num,
            "units": logic.UNITS,
            "locations": logic.LOCATIONS,
            "day_label": day_label,
            "keep_enabled": keep_sync.enabled() and is_keep_household(),
            "user": g.user,
            "household": g.household,
            "nav": request.endpoint,
            "storage_warning": not db.storage_is_persistent(),
            "storage_info": db.storage_info(),
        }

    @app.get("/healthz")
    def healthz():
        get_aconn().execute("SELECT 1")
        return {"ok": True, **db.storage_info()}

    # --------------------------------------------------------------------- Login

    def safe_next(default="/"):
        nxt = request.args.get("next", default)
        return nxt if nxt.startswith("/") and not nxt.startswith("//") else default

    def start_session(uid):
        session.clear()
        session.permanent = True
        session["uid"] = uid

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = accounts.authenticate(get_aconn(), request.form.get("name"), request.form.get("password"))
            if user:
                start_session(user["id"])
                return redirect(safe_next())
            flash("Name oder Passwort stimmt nicht.")
        return render_template("login.html")

    @app.route("/registrieren", methods=["GET", "POST"])
    def register():
        aconn = get_aconn()
        claimable = not accounts.is_claimed(aconn)  # bisheriger Schrank noch ohne Benutzer
        if request.method == "POST":
            f = request.form
            name, password = f.get("name", ""), f.get("password", "")
            mode = f.get("mode", "new")
            try:
                claim = f.get("claim_password", "")
                if mode == "join":
                    target = accounts.find_by_code(aconn, f.get("join_code"))
                    if not target:
                        raise ValueError("Diesen Beitrittscode gibt es nicht.")
                    uid = accounts.create_user(aconn, name, password, target["id"], "member")
                elif claimable and (claim or not app.config["APP_PASSWORD"]):
                    if app.config["APP_PASSWORD"] and not hmac.compare_digest(claim, app.config["APP_PASSWORD"]):
                        raise ValueError("Das bisherige App-Passwort stimmt nicht.")
                    uid = accounts.create_user(aconn, name, password, accounts.LEGACY_ID, "owner")
                else:
                    hid = accounts.create_household(
                        aconn, f.get("household_name") or f"Vorratsschrank von {name.strip()}")
                    try:
                        uid = accounts.create_user(aconn, name, password, hid, "owner")
                    except ValueError:
                        accounts.delete_household(aconn, hid)
                        raise
                    prepare_household(hid, sample=f.get("sample") == "1")
                start_session(uid)
                return redirect(url_for("index"))
            except ValueError as e:
                flash(str(e))
        return render_template("registrieren.html", claimable=claimable and bool(app.config["APP_PASSWORD"]),
                               form=request.form)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------ Benutzer und Vorratsschränke

    @app.get("/haushalt")
    @login_required
    def household_view():
        aconn = get_aconn()
        members = accounts.members(aconn, g.household["id"])
        return render_template("haushalt.html", members=members, code=accounts.show_code(g.household["join_code"]),
                               is_owner=g.user["role"] == "owner", sole=len(members) == 1)

    @app.post("/haushalt/name")
    @owner_required
    def household_rename():
        try:
            accounts.rename_household(get_aconn(), g.household["id"], request.form.get("name"))
            flash("Name gespeichert.")
        except ValueError as e:
            flash(str(e))
        return redirect(url_for("household_view"))

    @app.post("/haushalt/code")
    @owner_required
    def household_new_code():
        accounts.regenerate_code(get_aconn(), g.household["id"])
        flash("Neuer Beitrittscode erzeugt. Der alte gilt nicht mehr.")
        return redirect(url_for("household_view"))

    @app.post("/haushalt/beitreten")
    @login_required
    def household_join():
        aconn = get_aconn()
        target = accounts.find_by_code(aconn, request.form.get("join_code"))
        if not target:
            flash("Diesen Beitrittscode gibt es nicht.")
        elif target["id"] == g.household["id"]:
            flash("Du bist schon in diesem Vorratsschrank.")
        else:
            sole = len(accounts.members(aconn, g.household["id"])) == 1
            if sole and request.form.get("confirm") != "1":
                flash("Bitte bestätigen, dass dein bisheriger Vorratsschrank gelöscht wird.")
            else:
                old = g.household["id"]
                emptied = accounts.move_user(aconn, g.user["id"], target["id"])
                if emptied:
                    drop_household(old)
                flash(f"Du bist jetzt im Vorratsschrank „{target['name']}“.")
                return redirect(url_for("index"))
        return redirect(url_for("household_view"))

    @app.post("/haushalt/verlassen")
    @login_required
    def household_leave():
        aconn = get_aconn()
        if len(accounts.members(aconn, g.household["id"])) == 1:
            flash("Du bist allein in diesem Vorratsschrank – es gibt nichts zu verlassen.")
            return redirect(url_for("household_view"))
        hid = accounts.create_household(aconn, request.form.get("name") or f"Vorratsschrank von {g.user['name']}")
        accounts.move_user(aconn, g.user["id"], hid, "owner")
        prepare_household(hid, sample=request.form.get("sample") == "1")
        flash("Du hast jetzt einen eigenen, leeren Vorratsschrank.")
        return redirect(url_for("index"))

    @app.get("/sw.js")
    def service_worker():
        # Muss im Wurzelpfad liegen, damit er die ganze App abdeckt.
        resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---------------------------------------------------------------------- Heute

    @app.get("/")
    @login_required
    def index():
        conn = get_conn()
        logic.ensure_plan(conn)
        days = logic.plan(conn, days=7)
        today_entry = days[0] if days and days[0]["date"] == logic.today().isoformat() else None
        today_ings = logic.ingredients(conn, today_entry["recipe_id"]) if today_entry else []
        missing = [i for i in today_ings if i["stock"] + 1e-9 < i["per_portion"] * today_entry["portions"]] if today_entry else []
        open_count = conn.execute("SELECT COUNT(*) FROM shopping_items WHERE status='open'").fetchone()[0]
        return render_template(
            "index.html", today=today_entry, ings=today_ings, missing=missing,
            upcoming=[d for d in days if d is not today_entry], open_count=open_count,
            weekday_long=WEEKDAYS_LONG[logic.today().weekday()],
        )

    # ---------------------------------------------------------------------- Plan

    @app.get("/plan")
    @login_required
    def plan_view():
        conn = get_conn()
        logic.ensure_plan(conn, days=21)
        recipes = conn.execute(
            "SELECT id, name FROM recipes WHERE kind='meal' AND (active = 1 OR id IN "
            "(SELECT recipe_id FROM meal_plan WHERE date >= ?)) ORDER BY name",
            (logic.today().isoformat(),),
        ).fetchall()
        _, week = logic.rotation_recipe(conn, logic.today())
        return render_template("plan.html", days=logic.plan(conn, days=21), recipes=recipes, week=week)

    @app.post("/plan/<d>/cook")
    @login_required
    def plan_cook(d):
        logic.mark_cooked(get_conn(), d)
        flash("Als gekocht gebucht – Zutaten sind vom Vorrat abgezogen.")
        return redirect(request.referrer or url_for("index"))

    @app.post("/plan/<d>/skip")
    @login_required
    def plan_skip(d):
        logic.mark_skipped(get_conn(), d)
        return redirect(request.referrer or url_for("index"))

    @app.post("/plan/<d>/set")
    @login_required
    def plan_set(d):
        logic.set_plan(get_conn(), d, int(request.form["recipe_id"]),
                       float(request.form.get("portions") or 1))
        return redirect(url_for("plan_view"))

    # ------------------------------------------------------------------- Einkauf

    @app.get("/einkauf")
    @login_required
    def shopping():
        conn = get_conn()
        items = logic.shopping_list(conn)
        groups = {}
        for it in items:
            groups.setdefault(it["location"], []).append(it)
        return render_template(
            "einkauf.html", groups=groups, count=len(items),
            last_sync=db.get_setting(conn, "keep_last_sync"),
            last_error=db.get_setting(conn, "keep_last_error"),
            cost=logic.week_cost(conn),
        )

    @app.post("/einkauf/refresh")
    @login_required
    def shopping_refresh():
        hints = logic.refresh_shopping_list(get_conn())
        for h in hints:
            flash(h)
        flash("Liste für die nächsten Tage neu berechnet.")
        return redirect(url_for("shopping"))

    @app.post("/einkauf/add")
    @login_required
    def shopping_add():
        name = request.form.get("name", "").strip()
        qty = request.form.get("qty", "").replace(",", ".").strip()
        if name:
            logic.add_manual(get_conn(), name, float(qty) if qty else None)
        return redirect(url_for("shopping"))

    @app.post("/einkauf/<int:item_id>/bought")
    @login_required
    def shopping_bought(item_id):
        logic.mark_bought(get_conn(), item_id)
        return redirect(url_for("shopping"))

    @app.post("/einkauf/<int:item_id>/dismiss")
    @login_required
    def shopping_dismiss(item_id):
        logic.dismiss(get_conn(), item_id)
        return redirect(url_for("shopping"))

    @app.post("/einkauf/sync")
    @login_required
    def shopping_sync():
        conn = get_conn()
        if not (keep_sync.enabled() and is_keep_household()):
            flash("Google Keep ist nicht eingerichtet (KEEP_EMAIL / KEEP_MASTER_TOKEN fehlen) "
                  "oder gehört zu einem anderen Vorratsschrank.")
        else:
            try:
                s = keep_sync.run_once(conn)
                flash(f"Mit Keep abgeglichen: {s['added']} neu, {s['bought']} gekauft, "
                      f"{s['imported']} aus Keep übernommen.")
            except Exception as e:
                app.logger.exception("Keep-Sync")
                flash(f"Keep-Sync fehlgeschlagen: {e}")
        return redirect(url_for("shopping"))

    # --------------------------------------------------------------------- Vorrat

    @app.get("/vorrat")
    @login_required
    def stock():
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM products WHERE active = 1 ORDER BY "
            "CASE location WHEN 'Vorrat' THEN 0 WHEN 'Kühlschrank' THEN 1 WHEN 'TK' THEN 2 ELSE 3 END, "
            "category, name"
        ).fetchall()
        groups = {}
        for p in rows:
            groups.setdefault(p["location"], []).append(p)
        return render_template("vorrat.html", groups=groups)

    @app.post("/vorrat/inventur")
    @login_required
    def stock_inventory():
        conn = get_conn()
        changed = 0
        for key, val in request.form.items():
            if not key.startswith("stock_") or val.strip() == "":
                continue
            pid = int(key[6:])
            new = float(val.replace(",", "."))
            cur = conn.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()
            if cur and abs(cur["stock"] - new) > 1e-9:
                logic.set_stock(conn, pid, new)
                changed += 1
        flash(f"{changed} Bestände aktualisiert.")
        return redirect(url_for("stock"))

    @app.post("/vorrat/<int:pid>/adjust")
    @login_required
    def stock_adjust(pid):
        logic.change_stock(get_conn(), pid, float(request.form["delta"]), "Manuell")
        return redirect(url_for("stock") + f"#p{pid}")

    @app.get("/zutaten")
    @login_required
    def ingredients_view():
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM products ORDER BY active DESC, "
            "CASE location WHEN 'Vorrat' THEN 0 WHEN 'Kühlschrank' THEN 1 WHEN 'TK' THEN 2 ELSE 3 END, "
            "category, name"
        ).fetchall()
        groups = {}
        for p in rows:
            groups.setdefault("Ausgeblendet" if not p["active"] else p["location"], []).append(p)
        return render_template("zutaten.html", groups=groups)

    @app.route("/zutaten/<int:pid>", methods=["GET", "POST"])
    @login_required
    def ingredient_edit(pid):
        conn = get_conn()
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if not p:
            return redirect(url_for("ingredients_view"))
        if request.method == "POST":
            f = request.form

            def num_or_none(key):
                v = (f.get(key) or "").replace(",", ".").strip()
                return float(v) if v else None

            try:
                logic.update_product(
                    conn, pid, f.get("name"), f.get("unit"), f.get("location"), f.get("category", ""),
                    num_or_none("min_stock") or 0.0, num_or_none("pack_size"), num_or_none("price"),
                    active=f.get("active") == "1", factor=num_or_none("factor"),
                )
                flash("Zutat gespeichert.")
                return redirect(url_for("ingredients_view"))
            except ValueError as e:
                flash(str(e))
        used = conn.execute(
            "SELECT r.name FROM recipe_ingredients ri JOIN recipes r ON r.id = ri.recipe_id "
            "WHERE ri.product_id = ? ORDER BY r.name", (pid,)).fetchall()
        return render_template("zutat_edit.html", p=p, used=[r["name"] for r in used])

    @app.post("/vorrat/neu")
    @login_required
    def stock_new():
        f = request.form
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO products(name, unit, location, category, min_stock, pack_size) VALUES(?,?,?,?,?,?)",
                (f["name"].strip(), f["unit"], f["location"], f.get("category", "").strip(),
                 float(f.get("min_stock") or 0), float(f["pack_size"]) if f.get("pack_size") else None),
            )
            conn.commit()
            flash(f"{f['name']} angelegt.")
        except Exception as e:
            flash(f"Konnte Produkt nicht anlegen: {e}")
        return redirect(url_for("stock"))

    # -------------------------------------------------------------------- Rezepte

    @app.get("/rezepte")
    @login_required
    def recipes():
        conn = get_conn()
        rs = conn.execute("SELECT * FROM recipes ORDER BY active DESC, kind DESC, name").fetchall()
        return render_template(
            "rezepte.html", recipes=[(r, logic.ingredients(conn, r["id"])) for r in rs]
        )

    @app.route("/rezepte/import", methods=["GET", "POST"])
    @login_required
    def recipe_import():
        result = None
        if request.method == "POST":
            f = request.files.get("datei")
            if not f or not f.filename:
                flash("Bitte eine CSV-Datei auswählen.")
            else:
                try:
                    conn = get_conn()
                    recipes_, errors = recipe_csv.parse(recipe_csv.decode(f.read()))
                    deleted = None
                    if request.form.get("replace_all") == "1":
                        blocked = [f"{r['name']}: {m}" for r in recipes_ for m in recipe_csv.problems(conn, r)]
                        if request.form.get("confirm", "").strip().upper() != "LÖSCHEN":
                            flash("Zum Löschen bitte LÖSCHEN eintippen. Es wurde nichts verändert.")
                            return render_template("rezepte_import.html", result=None, columns=recipe_csv.COLUMNS,
                                                   instructions=recipe_csv.ai_instructions())
                        if not recipes_ or blocked:
                            result = {"created": [], "updated": [], "skipped": [], "deleted": None,
                                      "errors": errors + blocked + ["Nichts gelöscht und nichts importiert: "
                                                                    "erst die Fehler in der Datei beheben."]}
                            return render_template("rezepte_import.html", result=result, columns=recipe_csv.COLUMNS,
                                                   instructions=recipe_csv.ai_instructions())
                        deleted = logic.delete_all_recipes(conn)
                    result = recipe_csv.apply(conn, recipes_, overwrite=request.form.get("overwrite") == "1")
                    result["errors"] = errors + result["errors"]
                    result["deleted"] = deleted
                except recipe_csv.CsvError as e:
                    flash(str(e))
        return render_template("rezepte_import.html", result=result, columns=recipe_csv.COLUMNS,
                               instructions=recipe_csv.ai_instructions())

    @app.get("/rezepte/vorlage.csv")
    @login_required
    def recipe_template():
        return Response(recipe_csv.template_csv(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="rezepte-vorlage.csv"'})

    @app.get("/rezepte/ki-anleitung.txt")
    @login_required
    def recipe_ai_instructions():
        return Response(recipe_csv.ai_instructions(), mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="rezepte-ki-anleitung.txt"'})

    @app.post("/rezepte/<int:rid>/cook")
    @login_required
    def recipe_cook(rid):
        logic.cook_recipe(get_conn(), rid, float(request.form.get("portions") or 1))
        flash("Gekocht und gebucht.")
        return redirect(url_for("recipes"))

    @app.post("/rezepte/<int:rid>/active")
    @login_required
    def recipe_active(rid):
        active = request.form.get("active") == "1"
        logic.set_recipe_active(get_conn(), rid, active)
        flash("Rezept ist wieder in der Rotation." if active else "Rezept ist inaktiv und aus dem Plan genommen.")
        return redirect(url_for("recipes"))

    @app.route("/rezepte/<int:rid>/edit", methods=["GET", "POST"])
    @login_required
    def recipe_edit(rid):
        conn = get_conn()
        r = conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
        if not r:
            return redirect(url_for("recipes"))
        if request.method == "POST":
            ings = {}
            try:
                for i in logic.ingredients(conn, rid):
                    if request.form.get(f"del_{i['id']}"):
                        continue
                    ings[i["id"]] = float((request.form.get(f"qty_{i['id']}") or "0").replace(",", "."))
                new_name = (request.form.get("new_name") or "").strip()
                new_pid = request.form.get("new_product")
                if new_name or new_pid:
                    if new_name:
                        pack = request.form.get("new_pack_size")
                        new_pid = logic.create_product(
                            conn, new_name, request.form.get("new_unit", "g"),
                            request.form.get("new_location", "Vorrat"),
                            float(pack.replace(",", ".")) if pack else None,
                        )
                    ings[int(new_pid)] = float((request.form.get("new_qty") or "0").replace(",", "."))
                yq = request.form.get("yields_qty")
                logic.update_recipe(
                    conn, rid, request.form.get("name"), request.form.get("notes"), ings,
                    float(yq.replace(",", ".")) if yq else None,
                )
                flash("Rezept gespeichert.")
                return redirect(url_for("recipes"))
            except ValueError as e:
                flash(str(e))
        products = conn.execute(
            "SELECT id, name, unit FROM products WHERE active = 1 ORDER BY name"
        ).fetchall()
        return render_template("rezept_edit.html", r=r, ings=logic.ingredients(conn, rid), products=products)

    return app
