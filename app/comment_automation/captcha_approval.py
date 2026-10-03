from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import sqlite3


def _utc(value: str | None = None) -> datetime:
    parsed = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_captcha_approval_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS comment_captcha_approvals (
        token_hash TEXT PRIMARY KEY,
        comment_id INTEGER NOT NULL,
        professional_name TEXT NOT NULL,
        approver_user_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','CONSUMED','EXPIRED')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        approved_at TEXT,
        consumed_at TEXT,
        FOREIGN KEY(comment_id) REFERENCES comment_bank(id) ON DELETE CASCADE
    )""")


def create_captcha_approval(
    conn: sqlite3.Connection,
    comment_id: int,
    professional_name: str,
    approver_user_id: str,
    *,
    now: str | None = None,
    ttl_seconds: int = 300,
) -> str:
    created = _utc(now)
    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO comment_captcha_approvals
           (token_hash,comment_id,professional_name,approver_user_id,status,created_at,expires_at)
           VALUES (?,?,?,?, 'PENDING',?,?)""",
        (_token_hash(token), comment_id, professional_name, str(approver_user_id),
         _iso(created), _iso(created + timedelta(seconds=max(1, ttl_seconds)))),
    )
    return token


def approve_captcha_approval(
    conn: sqlite3.Connection, token: str, actor_user_id: str, *, now: str | None = None
) -> bool:
    if not token or not actor_user_id:
        return False
    instant = _iso(_utc(now))
    changed = conn.execute(
        """UPDATE comment_captcha_approvals SET status='APPROVED',approved_at=?
           WHERE token_hash=? AND approver_user_id=? AND status='PENDING' AND expires_at>?""",
        (instant, _token_hash(token), str(actor_user_id), instant),
    ).rowcount
    return changed == 1


def consume_captcha_approval(
    conn: sqlite3.Connection, token: str, *, now: str | None = None
) -> bool:
    if not token:
        return False
    instant = _iso(_utc(now))
    changed = conn.execute(
        """UPDATE comment_captcha_approvals SET status='CONSUMED',consumed_at=?
           WHERE token_hash=? AND status='APPROVED' AND expires_at>?""",
        (instant, _token_hash(token), instant),
    ).rowcount
    return changed == 1


def captcha_approval_status(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM comment_captcha_approvals WHERE token_hash=?",
        (_token_hash(token),),
    ).fetchone()
    return row["status"] if row else None


def expire_captcha_approval(
    conn: sqlite3.Connection, token: str, *, now: str | None = None
) -> None:
    conn.execute(
        """UPDATE comment_captcha_approvals SET status='EXPIRED'
           WHERE token_hash=? AND status IN ('PENDING','APPROVED') AND expires_at<=?""",
        (_token_hash(token), _iso(_utc(now))),
    )
