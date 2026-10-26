"""Zwei-Wege-Sync der Einkaufsliste mit einer Google-Keep-Liste.

Nutzt gkeepapi (inoffiziell!). Authentifizierung per Master-Token.
Empfehlung: eigenes Google-Konto nur für die App, Liste mit dem Hauptkonto teilen.

Regeln:
  App -> Keep   offene Einträge ohne keep_item_id werden in Keep angelegt,
                Textänderungen (Menge) werden übernommen,
                gekaufte/verworfene Einträge werden in Keep gelöscht.
  Keep -> App   in Keep abgehakt  -> in der App als gekauft gebucht (Bestand +),
                in Keep gelöscht  -> in der App verworfen,
                in Keep neu       -> als manueller Eintrag übernommen.
"""
import json
import logging
import os
import threading
import time

from . import db, logic

log = logging.getLogger("keep_sync")
_lock = threading.Lock()


def enabled() -> bool:
    return bool(os.environ.get("KEEP_EMAIL") and os.environ.get("KEEP_MASTER_TOKEN"))


def _state_file():
    return db.data_dir() / "keep_state.json"


def login():
    import gkeepapi  # nur importieren, wenn der Sync aktiv ist

    keep = gkeepapi.Keep()
    state = None
    sf = _state_file()
    if sf.exists():
        try:
            state = json.loads(sf.read_text())
        except Exception:
            state = None
    email, token = os.environ["KEEP_EMAIL"], os.environ["KEEP_MASTER_TOKEN"]
    if hasattr(keep, "authenticate"):
        keep.authenticate(email, token, state=state)
    else:  # ältere gkeepapi-Versionen
        keep.resume(email, token, state=state)
    return keep


def save_state(keep):
    try:
        _state_file().write_text(json.dumps(keep.dump()))
    except Exception as e:  # Cache ist optional
        log.warning("Keep-State nicht gespeichert: %s", e)


def get_list(conn, keep):
    title = os.environ.get("KEEP_LIST_TITLE", "Einkaufsliste")
    lid = db.get_setting(conn, "keep_list_id")
    glist = keep.get(lid) if lid else None
    if glist is None or getattr(glist, "trashed", False):
        glist = next(
            (n for n in keep.all() if getattr(n, "title", None) == title and hasattr(n, "items")
             and not getattr(n, "trashed", False)),
            None,
        )
    if glist is None:
        glist = keep.createList(title, [])
    db.set_setting(conn, "keep_list_id", glist.id)
    return glist


def sync_list(conn, glist) -> dict:
    """Reine Abgleichslogik, testbar mit Fake-Objekten (siehe tests/)."""
    stats = {"added": 0, "updated": 0, "bought": 0, "removed": 0, "imported": 0, "dismissed": 0}
    by_id = {it.id: it for it in glist.items}
    known = set()

    # 1) gekaufte / verworfene Einträge aus Keep entfernen
    for row in conn.execute(
        "SELECT * FROM shopping_items WHERE status != 'open' AND keep_item_id IS NOT NULL"
    ).fetchall():
        it = by_id.get(row["keep_item_id"])
        if it is not None:
            it.delete()
            stats["removed"] += 1
        known.add(row["keep_item_id"])
        conn.execute("UPDATE shopping_items SET keep_item_id = NULL WHERE id = ?", (row["id"],))

    # 2) offene, verknüpfte Einträge abgleichen
    for row in conn.execute(
        "SELECT * FROM shopping_items WHERE status = 'open' AND keep_item_id IS NOT NULL"
    ).fetchall():
        known.add(row["keep_item_id"])
        it = by_id.get(row["keep_item_id"])
        if it is None:
            logic.dismiss(conn, row["id"], commit=False)
            stats["dismissed"] += 1
        elif it.checked:
            logic.mark_bought(conn, row["id"], commit=False)
            it.delete()
            conn.execute("UPDATE shopping_items SET keep_item_id = NULL WHERE id = ?", (row["id"],))
            stats["bought"] += 1
        else:
            text = logic.item_text(row)
            if it.text != text:
                it.text = text
                stats["updated"] += 1

    # 3) neue Keep-Einträge übernehmen (bevor wir selbst welche anlegen)
    for it in list(glist.items):
        if it.id in known or getattr(it, "deleted", False):
            continue
        if it.checked:
            it.delete()
            continue
        name = (it.text or "").strip()
        if name:
            logic.add_manual(conn, name, source="keep", keep_item_id=it.id)
            stats["imported"] += 1

    # 4) offene App-Einträge ohne Verknüpfung in Keep anlegen
    for row in conn.execute(
        "SELECT * FROM shopping_items WHERE status = 'open' AND keep_item_id IS NULL"
    ).fetchall():
        new = glist.add(logic.item_text(row), False)
        conn.execute("UPDATE shopping_items SET keep_item_id = ? WHERE id = ?", (new.id, row["id"]))
        stats["added"] += 1

    conn.commit()
    return stats


def run_once(conn) -> dict:
    with _lock:
        keep = login()
        keep.sync()
        glist = get_list(conn, keep)
        stats = sync_list(conn, glist)
        keep.sync()
        save_state(keep)
        db.set_setting(conn, "keep_last_sync", time.strftime("%Y-%m-%d %H:%M:%S"))
        conn.commit()
        log.info("Keep-Sync: %s", stats)
        return stats


def start_background(make_conn):
    """Startet den periodischen Sync in einem Daemon-Thread (gunicorn: 1 Worker!)."""
    minutes = float(os.environ.get("KEEP_SYNC_INTERVAL_MIN", 10))
    if not enabled() or minutes <= 0:
        log.info("Keep-Sync deaktiviert")
        return None

    def loop():
        conn = make_conn()
        while True:
            try:
                logic.refresh_shopping_list(conn)
                run_once(conn)
            except Exception as e:
                log.exception("Keep-Sync fehlgeschlagen: %s", e)
                db.set_setting(conn, "keep_last_error", f"{time.strftime('%Y-%m-%d %H:%M')}: {e}")
                conn.commit()
            time.sleep(minutes * 60)

    t = threading.Thread(target=loop, name="keep-sync", daemon=True)
    t.start()
    return t
