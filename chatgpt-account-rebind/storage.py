from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .models import AccountStatus, MailboxStatus, ParsedAccount, ParsedMailbox
except ImportError:  # direct `uvicorn app:app` launch
    from models import AccountStatus, MailboxStatus, ParsedAccount, ParsedMailbox


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
              password TEXT NOT NULL DEFAULT '', totp TEXT NOT NULL DEFAULT '', access_url TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'ready', rebound_email TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT '',
              trial_status TEXT NOT NULL DEFAULT 'unknown', last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mailboxes (
              id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, password TEXT NOT NULL DEFAULT '', access_url TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'available', reserved_by INTEGER, used_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def log(self, level: str, event: str, message: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO logs(level,event,message,created_at) VALUES(?,?,?,?)", (level, event, message, now()))
            self.db.commit()

    def import_accounts(self, rows: list[ParsedAccount]) -> int:
        with self.lock:
            for row in rows:
                t = now()
                self.db.execute(
                    """INSERT INTO accounts(email,password,totp,access_url,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(email) DO UPDATE SET password=excluded.password,totp=excluded.totp,access_url=excluded.access_url,updated_at=excluded.updated_at""",
                    (row.email, row.password, row.totp, row.access_url, AccountStatus.READY, t, t),
                )
            self.db.commit()
        return len(rows)

    def import_mailboxes(self, rows: list[ParsedMailbox]) -> int:
        with self.lock:
            for row in rows:
                t = now()
                self.db.execute(
                    """INSERT INTO mailboxes(email,password,access_url,status,created_at,updated_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(email) DO UPDATE SET password=excluded.password,access_url=excluded.access_url,updated_at=excluded.updated_at""",
                    (row.email, row.password, row.access_url, MailboxStatus.AVAILABLE, t, t),
                )
            self.db.commit()
        return len(rows)

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT id,email,status,rebound_email,at,trial_status,last_error,updated_at FROM accounts ORDER BY id DESC")]

    def list_mailboxes(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT id,email,status,reserved_by,used_by,updated_at FROM mailboxes ORDER BY id DESC")]

    def list_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT level,event,message,created_at FROM logs ORDER BY id DESC LIMIT ?", (limit,))]

    def account_secret(self, account_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def update_account(self, account_id: int, **fields: Any) -> None:
        allowed = {"status", "rebound_email", "at", "trial_status", "last_error", "email", "password", "totp", "access_url"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = now()
        clause = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE accounts SET {clause} WHERE id=?", (*fields.values(), account_id))
            self.db.commit()

    def reserve_mailbox(self, account_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM mailboxes WHERE status='available' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE mailboxes SET status='reserved',reserved_by=?,updated_at=? WHERE id=?", (account_id, now(), row["id"]))
            self.db.commit()
            return dict(row)

    def mark_mailbox(self, mailbox_id: int, status: str, account_id: int | None = None) -> None:
        self.db.execute("UPDATE mailboxes SET status=?,used_by=?,updated_at=? WHERE id=?", (status, account_id, now(), mailbox_id))
        self.db.commit()
