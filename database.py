import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            telegram_id     INTEGER PRIMARY KEY,
            ig_user_id      TEXT NOT NULL,
            access_token    TEXT NOT NULL,
            ig_username     TEXT,
            connected_at    TEXT NOT NULL,
            token_expires_at TEXT,\n            auth_type       TEXT NOT NULL DEFAULT 'instagram'\n        );

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
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN token_expires_at TEXT")
        except sqlite3.OperationalError:
            pass


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_account(telegram_id: int, ig_user_id: str, access_token: str, ig_username: str, token_expires_at: str | None = None, auth_type: str = "instagram"):\n    with get_conn() as conn:\n        conn.execute("""\n            INSERT INTO accounts (telegram_id, ig_user_id, access_token, ig_username, connected_at, token_expires_at, auth_type)\n            VALUES (?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(telegram_id) DO UPDATE SET\n                ig_user_id=excluded.ig_user_id,\n                access_token=excluded.access_token,\n                ig_username=excluded.ig_username,\n                connected_at=excluded.connected_at,\n                token_expires_at=excluded.token_expires_at,\n                auth_type=excluded.auth_type\n        """, (telegram_id, ig_user_id, access_token, ig_username, datetime.utcnow().isoformat(), token_expires_at, auth_type))\ndef add_scheduled_post(telegram_id: int, media_type: str, media_urls_json: str,\n                        caption: str, publish_at_iso: str, media_file_ids_json: str | None = None,\n                        audio_id: str | None = None, audio_title: str | None = None) -> int:\n    with get_conn() as conn:\n        cur = conn.execute("""\n            INSERT INTO scheduled_posts\n                (telegram_id, media_type, media_urls, caption, publish_at, created_at, media_file_ids, audio_id, audio_title)\n            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n        """, (telegram_id, media_type, media_urls_json, caption, publish_at_iso,\n              datetime.utcnow().isoformat(), media_file_ids_json, audio_id, audio_title))\n        return cur.lastrowid\n
