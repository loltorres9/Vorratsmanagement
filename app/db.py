"""SQLite-Zugriff (nur Standardbibliothek). Eine Datei unter DATA_DIR."""
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    unit              TEXT NOT NULL,              -- g, ml, Stk, Portion
    location          TEXT NOT NULL,              -- Vorrat, Kühlschrank, TK
    category          TEXT NOT NULL DEFAULT '',   -- Sortierung der Einkaufsliste
    min_stock         REAL NOT NULL DEFAULT 0,    -- Mindestbestand (0 = nur nach Bedarf)
    pack_size         REAL,                       -- Packungsgröße zum Aufrunden
    price             REAL,                       -- Preis pro Packung (optional)
    stock             REAL NOT NULL DEFAULT 0,    -- aktueller Bestand in unit
    made_by_recipe_id INTEGER REFERENCES recipes(id), -- selbstgemacht (z.B. Gulasch-Portion)
    active            INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS recipes (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    kind              TEXT NOT NULL DEFAULT 'meal',  -- meal | batch
    yields_product_id INTEGER REFERENCES products(id),
    yields_qty        REAL,
    notes             TEXT NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 1,  -- 0 = nicht mehr in der Rotation
    servings          REAL NOT NULL DEFAULT 1,     -- Portionen, für die die Zutatenmengen gelten
    user_edited       INTEGER NOT NULL DEFAULT 0   -- 1 = in der App bearbeitet, Importe fassen es nicht mehr an
);
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty        REAL NOT NULL,                 -- Menge laut Rezept (für `servings` Portionen; bei batch: pro Batch)
    PRIMARY KEY (recipe_id, product_id)
);
CREATE TABLE IF NOT EXISTS rotation (
    week      INTEGER NOT NULL,   -- 1..n
    weekday   INTEGER NOT NULL,   -- 0 = Montag
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    PRIMARY KEY (week, weekday)
);
CREATE TABLE IF NOT EXISTS meal_plan (
    date      TEXT PRIMARY KEY,   -- ISO-Datum
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    portions  REAL NOT NULL DEFAULT 1,
    status    TEXT NOT NULL DEFAULT 'planned'   -- planned | cooked | skipped
);
CREATE TABLE IF NOT EXISTS shopping_items (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER REFERENCES products(id),
    name         TEXT NOT NULL,
    qty          REAL,
    unit         TEXT,
    packs        INTEGER,
    source       TEXT NOT NULL DEFAULT 'manual',  -- auto | manual | keep
    status       TEXT NOT NULL DEFAULT 'open',    -- open | bought | dismissed
    keep_item_id TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stock_log (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    product_id INTEGER NOT NULL REFERENCES products(id),
    delta      REAL NOT NULL,
    reason     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def data_dir() -> Path:
    p = Path(os.environ.get("DATA_DIR", "./data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def storage_is_persistent() -> bool:
    """False, wenn die App auf Railway läuft, aber DATA_DIR kein eingehängtes Volume ist.

    Dann liegt die Datenbank im Container und ist nach jedem Deploy wieder leer."""
    if not (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
            or os.environ.get("RAILWAY_SERVICE_ID")):
        return True
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return os.path.realpath(mount) == os.path.realpath(data_dir())
    return os.path.ismount(data_dir())


def db_path() -> Path:
    return Path(os.environ.get("DATABASE_PATH", data_dir() / "vorrat.db"))


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Spalten nachrüsten, die in älteren (bereits befüllten) Datenbanken fehlen."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)")}
    if "active" not in cols:
        conn.execute("ALTER TABLE recipes ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if "user_edited" not in cols:
        conn.execute("ALTER TABLE recipes ADD COLUMN user_edited INTEGER NOT NULL DEFAULT 0")
    if "servings" not in cols:
        conn.execute("ALTER TABLE recipes ADD COLUMN servings REAL NOT NULL DEFAULT 1")


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)),
    )
