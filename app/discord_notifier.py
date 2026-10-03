from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import settings
from .database import get_connection
from .services import batch_status


log = logging.getLogger("discord_notifier")


def _ensure_state_table():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )


def _get_state(key: str):
    _ensure_state_table()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key=?",
            (key,),
        ).fetchone()

        return row["value"] if row else None


def _set_state(key: str, value: str):
    _ensure_state_table()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_state(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def _notification_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_notification_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            last_error TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_notifications_due "
        "ON discord_notifications(status, next_attempt_at)"
    )


def enqueue_notification(conn, event_key: str, message: str, *, now: str | None = None) -> bool:
    if not event_key or not message:
        raise ValueError("A notificação precisa de event_key e message.")
    ensure_notification_schema(conn)
    timestamp = now or _notification_now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO discord_notifications(
            event_key, message, status, attempts, next_attempt_at,
            created_at, updated_at
        ) VALUES (?, ?, 'PENDING', 0, ?, ?, ?)
        """,
        (event_key, message, timestamp, timestamp, timestamp),
    )
    return cursor.rowcount == 1


def queue_discord_notification(event_key: str, message: str) -> bool:
    with get_connection() as conn:
        return enqueue_notification(conn, event_key, message)


def list_due_notifications(conn, now: str, *, limit: int = 10) -> list[dict]:
    ensure_notification_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM discord_notifications
        WHERE status='PENDING' AND next_attempt_at <= ?
        ORDER BY id
        LIMIT ?
        """,
        (now, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_notification_failed(conn, notification_id: int, error: str, *, now: str | None = None) -> str:
    ensure_notification_schema(conn)
    row = conn.execute(
        "SELECT attempts FROM discord_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    if not row:
        raise ValueError("Notificação inexistente.")
    attempts = int(row["attempts"]) + 1
    timestamp = now or _notification_now()
    retry_at = (
        datetime.fromisoformat(timestamp)
        + timedelta(seconds=min(300, 5 * (2 ** min(attempts - 1, 6))))
    ).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE discord_notifications
        SET attempts=?, last_error=?, next_attempt_at=?, updated_at=?
        WHERE id=?
        """,
        (attempts, str(error)[:1000], retry_at, timestamp, notification_id),
    )
    return retry_at


def mark_notification_sent(conn, notification_id: int, *, now: str | None = None) -> None:
    ensure_notification_schema(conn)
    timestamp = now or _notification_now()
    conn.execute(
        """
        UPDATE discord_notifications
        SET status='SENT', sent_at=?, updated_at=?
        WHERE id=?
        """,
        (timestamp, timestamp, notification_id),
    )


def deliver_pending_discord_notifications(limit: int = 1) -> int:
    if not settings.discord_webhook_url:
        return 0
    sent_count = 0
    with get_connection() as conn:
        for row in list_due_notifications(conn, _notification_now(), limit=limit):
            try:
                if not send_discord_sync(row["message"]):
                    continue
            except Exception as exc:
                mark_notification_failed(conn, row["id"], f"{type(exc).__name__}: {exc}")
                log.exception("Falha na entrega Discord da notificação %s.", row["event_key"])
            else:
                mark_notification_sent(conn, row["id"])
                sent_count += 1
    return sent_count


def _send_sync(message: str):
    payload = json.dumps(
        {
            "content": message,
            "allowed_mentions": {
                "parse": [],
            },
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        settings.discord_webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MesLibertinesManager/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(
        req,
        timeout=15,
    ) as response:
        response.read()


def send_discord_sync(message: str) -> bool:
    if not settings.discord_webhook_url:
        return False

    _send_sync(message)
    return True


async def send_discord(message: str):
    return await asyncio.to_thread(
        send_discord_sync,
        message,
    )


async def check_batch_notification():
    if not settings.discord_webhook_url:
        return

    status = batch_status()

    # Não avisa antes de existir pelo menos um lote anterior.
    batch_id = status["last_batch_started_at"] or "sem-lote-anterior"

    if not status["available"]:
        return

    notification_key = (
        f"{status['today']}:{status['direct_capacity']}:"
        f"{status['vpn_capacity']}:{batch_id}"
    )
    already_notified = _get_state("last_discord_batch_notification")
    if already_notified == notification_key:
        return

    amount = status["available_capacity"]
    message = (
        "✅ **CAPACIDADE DE CADASTRO DISPONÍVEL**\n"
        f"Dia de controle: {status['today']}\n"
        f"Pendentes: {status['pending']}\n"
        f"Rota direta disponível: {status['direct_capacity']}\n"
        f"VPN autorizada disponível: {status['vpn_capacity']}\n"
        f"Você pode preparar agora até **{amount} contas**."
    )

    try:
        sent = await send_discord(message)

        if sent:
            _set_state(
                "last_discord_batch_notification",
                notification_key,
            )

            log.info(
                "Notificação de novo lote enviada ao Discord."
            )

    except Exception:
        log.exception(
            "Falha ao enviar notificação ao Discord."
        )


async def discord_watch_loop():
    while True:
        try:
            await asyncio.to_thread(deliver_pending_discord_notifications, 1)
            await check_batch_notification()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Erro no monitor do Discord."
            )

        await asyncio.sleep(settings.discord_notification_poll_seconds)
