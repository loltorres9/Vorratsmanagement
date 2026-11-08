"""Benutzer und Vorratsschränke (Haushalte) – ohne Flask-Abhängigkeit.

Jeder Vorratsschrank hat eine eigene SQLite-Datei mit dem unveränderten Schema aus db.py
(Haushalt 1 = die bisherige Datenbank). Diese Datei hier verwaltet nur, wer zu welchem
Schrank gehört: `accounts.db` mit den Tabellen `households` und `users`.
"""
import secrets
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

LEGACY_ID = 1  # der Schrank, der aus der Zeit vor den Benutzerkonten stammt (Keep-Sync läuft nur dort)
MIN_PASSWORD = 8
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O/1/I
CODE_LEN = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    join_code  TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    household_id  INTEGER NOT NULL REFERENCES households(id),
    role          TEXT NOT NULL DEFAULT 'member',   -- owner | member
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------- Join-Code

def new_code(conn) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if not conn.execute("SELECT 1 FROM households WHERE join_code = ?", (code,)).fetchone():
            return code


def normalize_code(text) -> str:
    return "".join(c for c in (text or "").upper() if c.isalnum())


def show_code(code) -> str:
    """XXXX-XXXX – besser lesbar und diktierbar."""
    return f"{code[:CODE_LEN // 2]}-{code[CODE_LEN // 2:]}"


def find_by_code(conn, text):
    code = normalize_code(text)
    if not code:
        return None
    return conn.execute("SELECT * FROM households WHERE join_code = ?", (code,)).fetchone()


# ------------------------------------------------------------------- Haushalte

def ensure_legacy_household(conn) -> bool:
    """Legt Haushalt 1 an (mit der bestehenden Datenbank), falls es noch gar keinen gibt."""
    if conn.execute("SELECT 1 FROM households LIMIT 1").fetchone():
        return False
    conn.execute("INSERT INTO households(id, name, join_code) VALUES(?,?,?)",
                 (LEGACY_ID, "Mein Vorratsschrank", new_code(conn)))
    conn.commit()
    return True


def create_household(conn, name) -> int:
    name = (name or "").strip() or "Mein Vorratsschrank"
    cur = conn.execute("INSERT INTO households(name, join_code) VALUES(?,?)", (name[:60], new_code(conn)))
    conn.commit()
    return cur.lastrowid


def get_household(conn, hid):
    return conn.execute("SELECT * FROM households WHERE id = ?", (hid,)).fetchone()


def all_households(conn):
    return conn.execute("SELECT * FROM households ORDER BY id").fetchall()


def rename_household(conn, hid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Der Name fehlt.")
    conn.execute("UPDATE households SET name = ? WHERE id = ?", (name[:60], hid))
    conn.commit()


def regenerate_code(conn, hid) -> str:
    code = new_code(conn)
    conn.execute("UPDATE households SET join_code = ? WHERE id = ?", (code, hid))
    conn.commit()
    return code


def delete_household(conn, hid) -> None:
    conn.execute("DELETE FROM households WHERE id = ?", (hid,))
    conn.commit()


def is_claimed(conn, hid=LEGACY_ID) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE household_id = ?", (hid,)).fetchone() is not None


# -------------------------------------------------------------------- Benutzer

def create_user(conn, name, password, household_id, role="member") -> int:
    """Wirft ValueError bei ungültigen Angaben."""
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise ValueError("Bitte einen Benutzernamen mit bis zu 40 Zeichen angeben.")
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Das Passwort braucht mindestens {MIN_PASSWORD} Zeichen.")
    try:
        cur = conn.execute(
            "INSERT INTO users(name, password_hash, household_id, role) VALUES(?,?,?,?)",
            (name, generate_password_hash(password), household_id, role),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"Den Benutzernamen „{name}“ gibt es schon.")
    conn.commit()
    return cur.lastrowid


def authenticate(conn, name, password):
    row = conn.execute("SELECT * FROM users WHERE name = ?", ((name or "").strip(),)).fetchone()
    if row and check_password_hash(row["password_hash"], password or ""):
        return row
    return None


def get_user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def members(conn, hid):
    return conn.execute(
        "SELECT * FROM users WHERE household_id = ? ORDER BY role = 'owner' DESC, id", (hid,)
    ).fetchall()


def move_user(conn, uid, new_hid, role="member") -> bool:
    """Setzt den Benutzer in einen anderen Schrank. Im alten Schrank rückt bei Bedarf das
    dienstälteste Mitglied zum Besitzer auf. Liefert True, wenn der alte Schrank jetzt leer ist."""
    old = get_user(conn, uid)["household_id"]
    conn.execute("UPDATE users SET household_id = ?, role = ? WHERE id = ?", (new_hid, role, uid))
    left = members(conn, old)
    if left and not any(m["role"] == "owner" for m in left):
        conn.execute("UPDATE users SET role = 'owner' WHERE id = ?", (left[0]["id"],))
    conn.commit()
    return not left
