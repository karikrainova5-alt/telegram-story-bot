import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = "bot.db"


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            telegram_id     INTEGER PRIMARY KEY,
            ig_user_id      TEXT NOT NULL,
            access_token    TEXT NOT NULL,
            ig_username     TEXT,
            connected_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER NOT NULL,
            media_type      TEXT NOT NULL,
            media_urls      TEXT NOT NULL,
            caption         TEXT,
            publish_at      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            error_message   TEXT,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_access (
            telegram_id         INTEGER PRIMARY KEY,
            trial_used          INTEGER NOT NULL DEFAULT 0,
            subscription_until  TEXT,
            updated_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            charge_id    TEXT PRIMARY KEY,
            telegram_id  INTEGER NOT NULL,
            amount       INTEGER NOT NULL,
            currency     TEXT NOT NULL,
            paid_at      TEXT NOT NULL
        );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_account(telegram_id: int, ig_user_id: str, access_token: str, ig_username: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO accounts (telegram_id, ig_user_id, access_token, ig_username, connected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                ig_user_id=excluded.ig_user_id,
                access_token=excluded.access_token,
                ig_username=excluded.ig_username,
                connected_at=excluded.connected_at
        """, (telegram_id, ig_user_id, access_token, ig_username, datetime.utcnow().isoformat()))


def get_account(telegram_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE telegram_id=?", (telegram_id,)).fetchone()
        return dict(row) if row else None


def delete_account(telegram_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE telegram_id=?", (telegram_id,))


def add_scheduled_post(telegram_id: int, media_type: str, media_urls_json: str,
                        caption: str, publish_at_iso: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO scheduled_posts
                (telegram_id, media_type, media_urls, caption, publish_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (telegram_id, media_type, media_urls_json, caption, publish_at_iso,
              datetime.utcnow().isoformat()))
        return cur.lastrowid


def get_due_posts(now_iso: str):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM scheduled_posts
            WHERE status='pending' AND publish_at<=?
        """, (now_iso,)).fetchall()
        return [dict(r) for r in rows]


def mark_post_done(post_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE scheduled_posts SET status='done' WHERE id=?", (post_id,))


def mark_post_error(post_id: int, message: str):
    with get_conn() as conn:
        conn.execute("""
            UPDATE scheduled_posts SET status='error', error_message=? WHERE id=?
        """, (message, post_id))


def get_user_posts(telegram_id: int):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM scheduled_posts WHERE telegram_id=? ORDER BY publish_at DESC LIMIT 20
        """, (telegram_id,)).fetchall()
        return [dict(r) for r in rows]


def cancel_post(post_id: int, telegram_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("""
            DELETE FROM scheduled_posts WHERE id=? AND telegram_id=? AND status='pending'
        """, (post_id, telegram_id))
        return cur.rowcount > 0


def get_access(telegram_id: int):
    """Return trial/subscription status for a user."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_access WHERE telegram_id=?",
            (telegram_id,)
        ).fetchone()
        if not row:
            return {"trial_used": False, "subscription_until": None, "active": False}
        until = row["subscription_until"]
        active = False
        if until:
            try:
                active = datetime.fromisoformat(until) > datetime.utcnow()
            except ValueError:
                active = False
        return {
            "trial_used": bool(row["trial_used"]),
            "subscription_until": until,
            "active": active,
        }


def consume_trial(telegram_id: int):
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute("""
            INSERT INTO user_access (telegram_id, trial_used, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                trial_used=1,
                updated_at=excluded.updated_at
        """, (telegram_id, now))


def activate_subscription(telegram_id: int, days: int = 30):
    with get_conn() as conn:
        now = datetime.utcnow()
        row = conn.execute(
            "SELECT subscription_until FROM user_access WHERE telegram_id=?",
            (telegram_id,)
        ).fetchone()
        current_until = None
        if row and row["subscription_until"]:
            try:
                current_until = datetime.fromisoformat(row["subscription_until"])
            except ValueError:
                current_until = None

        start = current_until if current_until and current_until > now else now
        new_until = start + __import__("datetime").timedelta(days=days)
        conn.execute("""
            INSERT INTO user_access (telegram_id, trial_used, subscription_until, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                trial_used=1,
                subscription_until=excluded.subscription_until,
                updated_at=excluded.updated_at
        """, (telegram_id, new_until.isoformat(), now.isoformat()))
        return new_until


def record_payment(charge_id: str, telegram_id: int, amount: int, currency: str) -> bool:
    with get_conn() as conn:
        try:
            conn.execute("""
                INSERT INTO payments (charge_id, telegram_id, amount, currency, paid_at)
                VALUES (?, ?, ?, ?, ?)
            """, (charge_id, telegram_id, amount, currency, datetime.utcnow().isoformat()))
            return True
        except sqlite3.IntegrityError:
            return False
