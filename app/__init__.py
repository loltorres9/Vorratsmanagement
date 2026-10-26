"""Vorrat – Inventar, Speiseplan und Einkaufsliste für einen Haushalt."""
import hmac
import logging
import os
from datetime import date, timedelta
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, send_from_directory, session, url_for

from . import db, keep_sync, logic, seed

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
    )
    if test_config:
        app.config.update(test_config)

    if not app.config["APP_PASSWORD"]:
        app.logger.warning("APP_PASSWORD ist nicht gesetzt – Login ist deaktiviert (nur lokal verwenden!)")

    def make_conn():
        return db.connect(app.config["DATABASE_PATH"])

    with app.app_context():
        conn = make_conn()
        db.init(conn)
        if seed.ensure_seeded(conn):
            app.logger.info("Startdaten geladen")
        if seed.apply_recipe_import(conn):
            app.logger.info("Rezepte aus dem Rezeptordner übernommen")
        logic.ensure_plan(conn)
        conn.close()

    if app.config["START_SYNC"]:
        keep_sync.start_background(make_conn)

    # ------------------------------------------------------------- Infrastruktur

    def get_conn():
        if "conn" not in g:
            g.conn = make_conn()
        return g.conn

    @app.teardown_appcontext
    def close_conn(exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if app.config["APP_PASSWORD"] and not session.get("ok"):
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapped

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
            "day_label": day_label,
            "keep_enabled": keep_sync.enabled(),
            "nav": request.endpoint,
        }

    @app.get("/healthz")
    def healthz():
        get_conn().execute("SELECT 1")
        return {"ok": True}

    # --------------------------------------------------------------------- Login

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            pw = request.form.get("password", "")
            if app.config["APP_PASSWORD"] and hmac.compare_digest(pw, app.config["APP_PASSWORD"]):
                session.permanent = True
                session["ok"] = True
                nxt = request.args.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash("Passwort stimmt nicht.")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

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
        missing = [i for i in today_ings if i["stock"] + 1e-9 < i["qty"] * today_entry["portions"]] if today_entry else []
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
        if not keep_sync.enabled():
            flash("Google Keep ist nicht eingerichtet (KEEP_EMAIL / KEEP_MASTER_TOKEN fehlen).")
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
                new_pid = request.form.get("new_product")
                if new_pid:
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
