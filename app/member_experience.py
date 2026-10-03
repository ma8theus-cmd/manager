from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from contextlib import nullcontext
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import settings
from .database import get_connection, transaction, utc_now


BATCH_DRAFT = "DRAFT"
BATCH_RUNNING = "RUNNING"
BATCH_COMPLETED = "COMPLETED"
BATCH_FAILED = "FAILED"

ITEM_QUEUED = "QUEUED"
ITEM_RUNNING = "RUNNING"
ITEM_CLICKED = "CLICKED"
ITEM_COMPLETED = "COMPLETED"
ITEM_FAILED = "FAILED"


def init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS member_experience_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            professional_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            last_error TEXT,
            FOREIGN KEY(professional_id)
                REFERENCES comment_professionals(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS member_experience_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            professional_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            target_url TEXT NOT NULL,
            position INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            login_at TEXT,
            click_at TEXT,
            logout_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(batch_id, member_id),
            FOREIGN KEY(batch_id)
                REFERENCES member_experience_batches(id) ON DELETE CASCADE,
            FOREIGN KEY(professional_id)
                REFERENCES comment_professionals(id) ON DELETE CASCADE,
            FOREIGN KEY(member_id)
                REFERENCES members(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS member_experience_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            queue_id INTEGER,
            event TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id)
                REFERENCES member_experience_batches(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_id)
                REFERENCES member_experience_queue(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_member_experience_batch
        ON member_experience_queue(batch_id, position);

        CREATE INDEX IF NOT EXISTS idx_member_experience_status
        ON member_experience_queue(status, updated_at);
        """
    )


def _ordered_ids(member_ids: Iterable[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw_id in member_ids:
        member_id = int(raw_id)
        if member_id <= 0:
            raise ValueError("ID de membro inválido.")
        if member_id not in seen:
            seen.add(member_id)
            result.append(member_id)
    if not result:
        raise ValueError("Selecione ao menos um membro.")
    return result


def eligible_members_for_professional(conn, professional_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            m.id, m.first_name, m.last_name, m.username, m.email,
            p.id AS professional_id, p.name AS professional_name,
            p.target_url
        FROM comment_professionals p
        CROSS JOIN members m
        WHERE p.id=?
          AND p.enabled=1
          AND m.profile_status='COMPLETE'
          AND m.username IS NOT NULL
          AND COALESCE(m.top50_eligibility, 'ELIGIBLE')='ELIGIBLE'
          AND NOT EXISTS (
              SELECT 1
              FROM member_experience_queue used
              WHERE used.professional_id=p.id
                AND used.member_id=m.id
                AND used.status IN ('QUEUED', 'RUNNING', 'CLICKED', 'COMPLETED')
          )
        ORDER BY m.id
        """,
        (professional_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def prepare_batch(
    conn,
    professional_id: int,
    member_ids: Iterable[int],
    *,
    now: str | None = None,
) -> int:
    init_schema(conn)
    selected_ids = _ordered_ids(member_ids)
    professional = conn.execute(
        """
        SELECT id, name, target_url
        FROM comment_professionals
        WHERE id=? AND enabled=1
        """,
        (professional_id,),
    ).fetchone()
    if not professional:
        raise ValueError("Profissional não encontrada ou inativa.")
    target_url = (professional["target_url"] or "").strip()
    if not target_url.startswith(("http://", "https://")):
        raise ValueError("Cadastre a URL do anúncio antes de preparar a fila.")

    eligible = {
        row["id"]: row
        for row in eligible_members_for_professional(conn, professional_id)
    }
    missing = [str(member_id) for member_id in selected_ids if member_id not in eligible]
    if missing:
        raise ValueError(
            "Membro(s) sem feedback positivo confirmado para esta profissional: "
            + ", ".join(missing)
        )

    timestamp = now or utc_now()
    batch_cursor = conn.execute(
        """
        INSERT INTO member_experience_batches(
            professional_id, status, created_at
        ) VALUES (?, ?, ?)
        """,
        (professional_id, BATCH_DRAFT, timestamp),
    )
    batch_id = int(batch_cursor.lastrowid)

    for position, member_id in enumerate(selected_ids, start=1):
        conn.execute(
            """
            INSERT INTO member_experience_queue(
                batch_id, professional_id, member_id, target_url,
                position, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                professional_id,
                member_id,
                target_url,
                position,
                ITEM_QUEUED,
                timestamp,
                timestamp,
            ),
        )

    _audit(
        conn,
        batch_id=batch_id,
        event="BATCH_PREPARED",
        details=f"{len(selected_ids)} membro(s) selecionado(s); execução não iniciada.",
        created_at=timestamp,
    )
    return batch_id


def start_batch(conn, batch_id: int, *, now: str | None = None) -> None:
    init_schema(conn)
    timestamp = now or utc_now()
    batch = conn.execute(
        "SELECT id, status FROM member_experience_batches WHERE id=?",
        (batch_id,),
    ).fetchone()
    if not batch:
        raise ValueError("Lote de experiências não encontrado.")
    if batch["status"] != BATCH_DRAFT:
        raise ValueError("Este lote já foi iniciado ou encerrado.")
    count = conn.execute(
        "SELECT COUNT(*) FROM member_experience_queue WHERE batch_id=?",
        (batch_id,),
    ).fetchone()[0]
    if not count:
        raise ValueError("O lote não possui membros selecionados.")

    conn.execute(
        """
        UPDATE member_experience_batches
        SET status=?, started_at=?, last_error=NULL
        WHERE id=?
        """,
        (BATCH_RUNNING, timestamp, batch_id),
    )
    _audit(
        conn,
        batch_id=batch_id,
        event="BATCH_STARTED",
        details="Início explícito solicitado no painel.",
        created_at=timestamp,
    )


def recover_interrupted_items(
    conn,
    batch_id: int,
    *,
    now: str | None = None,
) -> int:
    """Requeue items abandoned by an executor crash before Ajouter was clicked."""
    init_schema(conn)
    timestamp = now or utc_now()
    rows = conn.execute(
        """
        SELECT id
        FROM member_experience_queue
        WHERE batch_id=? AND status=? AND click_at IS NULL
        ORDER BY position
        """,
        (batch_id, ITEM_RUNNING),
    ).fetchall()
    if not rows:
        return 0
    conn.execute(
        """
        UPDATE member_experience_queue
        SET status=?, last_error=NULL, updated_at=?
        WHERE batch_id=? AND status=? AND click_at IS NULL
        """,
        (ITEM_QUEUED, timestamp, batch_id, ITEM_RUNNING),
    )
    _audit(
        conn,
        batch_id=batch_id,
        event="ITEMS_RECOVERED",
        details=f"{len(rows)} item(ns) reencaminhado(s) após interrupção do executor.",
        created_at=timestamp,
    )
    return len(rows)


def _audit(
    conn,
    *,
    batch_id: int,
    event: str,
    details: str = "",
    queue_id: int | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO member_experience_audit(
            batch_id, queue_id, event, details, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (batch_id, queue_id, event, details[:2000], created_at or utc_now()),
    )


def claim_next_item(batch_id: int) -> dict[str, Any] | None:
    with transaction(immediate=True) as conn:
        batch = conn.execute(
            "SELECT status FROM member_experience_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if not batch or batch["status"] != BATCH_RUNNING:
            return None
        row = conn.execute(
            """
            SELECT q.id, q.batch_id, q.member_id, q.target_url, q.position,
                   m.first_name, m.last_name, m.username, m.email,
                   m.birth_date, p.name AS professional_name
            FROM member_experience_queue q
            JOIN members m ON m.id=q.member_id
            JOIN comment_professionals p ON p.id=q.professional_id
            WHERE q.batch_id=? AND q.status=?
            ORDER BY q.position
            LIMIT 1
            """,
            (batch_id, ITEM_QUEUED),
        ).fetchone()
        if not row:
            return None
        timestamp = utc_now()
        conn.execute(
            """
            UPDATE member_experience_queue
            SET status=?, updated_at=?
            WHERE id=? AND status=?
            """,
            (ITEM_RUNNING, timestamp, row["id"], ITEM_QUEUED),
        )
        _audit(
            conn,
            batch_id=batch_id,
            queue_id=row["id"],
            event="ITEM_CLAIMED",
            details=f"Posição {row['position']} reservada pelo executor.",
            created_at=timestamp,
        )
        return dict(row)


def record_login(
    queue_id: int,
    *,
    now: str | None = None,
    conn=None,
) -> None:
    with (nullcontext(conn) if conn is not None else transaction(immediate=True)) as conn:
        row = conn.execute(
            "SELECT id, batch_id, status FROM member_experience_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not row or row["status"] != ITEM_RUNNING:
            raise ValueError("Item não está em execução.")
        timestamp = now or utc_now()
        conn.execute(
            "UPDATE member_experience_queue SET login_at=?, updated_at=? WHERE id=?",
            (timestamp, timestamp, queue_id),
        )
        _audit(
            conn,
            batch_id=row["batch_id"],
            queue_id=queue_id,
            event="LOGIN_CONFIRMED",
            details="Login confirmado pelo site.",
            created_at=timestamp,
        )


def record_click(
    queue_id: int,
    *,
    now: str | None = None,
    conn=None,
) -> bool:
    with (nullcontext(conn) if conn is not None else transaction(immediate=True)) as conn:
        row = conn.execute(
            "SELECT id, batch_id, status FROM member_experience_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not row or row["status"] != ITEM_RUNNING:
            raise ValueError("O login precisa ser confirmado antes do Ajouter.")
        timestamp = now or utc_now()
        conn.execute(
            """
            UPDATE member_experience_queue
            SET status=?, click_at=?, updated_at=?
            WHERE id=?
            """,
            (ITEM_CLICKED, timestamp, timestamp, queue_id),
        )
        _audit(
            conn,
            batch_id=row["batch_id"],
            queue_id=queue_id,
            event="AJOUTER_CLICKED",
            details="Clique no Ajouter confirmado pelo executor.",
            created_at=timestamp,
        )

        remaining = conn.execute(
            """
            SELECT COUNT(*)
            FROM member_experience_queue
            WHERE batch_id=? AND status NOT IN (?, ?)
            """,
            (row["batch_id"], ITEM_CLICKED, ITEM_COMPLETED),
        ).fetchone()[0]
        if remaining:
            return False

        _audit(
            conn,
            batch_id=row["batch_id"],
            event="BATCH_ALL_CLICKED",
            details="Todos os membros associados clicaram no Ajouter.",
            created_at=timestamp,
        )
        return True


def record_logout(
    queue_id: int,
    *,
    now: str | None = None,
    conn=None,
) -> bool:
    with (nullcontext(conn) if conn is not None else transaction(immediate=True)) as conn:
        row = conn.execute(
            "SELECT id, batch_id, status FROM member_experience_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not row or row["status"] != ITEM_CLICKED:
            raise ValueError("O Ajouter precisa ser registrado antes do logout.")
        timestamp = now or utc_now()
        conn.execute(
            """
            UPDATE member_experience_queue
            SET status=?, logout_at=?, updated_at=?
            WHERE id=?
            """,
            (ITEM_COMPLETED, timestamp, timestamp, queue_id),
        )
        _audit(
            conn,
            batch_id=row["batch_id"],
            queue_id=queue_id,
            event="LOGOUT_CONFIRMED",
            details="Logout confirmado pelo site.",
            created_at=timestamp,
        )
        remaining = conn.execute(
            """
            SELECT COUNT(*) FROM member_experience_queue
            WHERE batch_id=? AND status<>?
            """,
            (row["batch_id"], ITEM_COMPLETED),
        ).fetchone()[0]
        if remaining:
            return False
        conn.execute(
            """
            UPDATE member_experience_batches
            SET status=?, completed_at=?, last_error=NULL
            WHERE id=? AND status=?
            """,
            (BATCH_COMPLETED, timestamp, row["batch_id"], BATCH_RUNNING),
        )
        _audit(
            conn,
            batch_id=row["batch_id"],
            event="BATCH_COMPLETED",
            details="Todos os membros associados concluíram o Ajouter.",
            created_at=timestamp,
        )
        return True


def record_failure(
    queue_id: int,
    error: str,
    *,
    now: str | None = None,
) -> None:
    with transaction(immediate=True) as conn:
        row = conn.execute(
            """
            SELECT id, batch_id, status
            FROM member_experience_queue
            WHERE id=?
            """,
            (queue_id,),
        ).fetchone()
        if not row:
            return
        timestamp = now or utc_now()
        conn.execute(
            """
            UPDATE member_experience_queue
            SET status=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (ITEM_FAILED, error[:1000], timestamp, queue_id),
        )
        conn.execute(
            """
            UPDATE member_experience_batches
            SET status=?, last_error=?
            WHERE id=? AND status=?
            """,
            (BATCH_FAILED, error[:1000], row["batch_id"], BATCH_RUNNING),
        )
        _audit(
            conn,
            batch_id=row["batch_id"],
            queue_id=queue_id,
            event="ITEM_FAILED",
            details=error,
            created_at=timestamp,
        )


def mark_batch_launch_failed(batch_id: int, error: str) -> None:
    with transaction(immediate=True) as conn:
        timestamp = utc_now()
        conn.execute(
            """
            UPDATE member_experience_batches
            SET status=?, last_error=?
            WHERE id=? AND status=?
            """,
            (BATCH_FAILED, error[:1000], batch_id, BATCH_RUNNING),
        )
        _audit(
            conn,
            batch_id=batch_id,
            event="BATCH_LAUNCH_FAILED",
            details=error,
            created_at=timestamp,
        )


def batch_dashboard_data() -> dict[str, Any]:
    with get_connection() as conn:
        init_schema(conn)
        professional_rows = conn.execute(
            """
            SELECT id, name, target_url, enabled
            FROM comment_professionals
            WHERE feedback_kind='POSITIVE' AND enabled=1
            ORDER BY id
            """
        ).fetchall()
        professionals: list[dict[str, Any]] = []
        for professional in professional_rows:
            item = dict(professional)
            item["eligible_members"] = eligible_members_for_professional(
                conn, professional["id"]
            )
            batches = conn.execute(
                """
                SELECT id, status, created_at, started_at, completed_at, last_error
                FROM member_experience_batches
                WHERE professional_id=?
                ORDER BY id DESC
                LIMIT 5
                """,
                (professional["id"],),
            ).fetchall()
            item["batches"] = []
            for batch in batches:
                batch_data = dict(batch)
                batch_data["items"] = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT q.id, q.member_id, q.position, q.status,
                               q.login_at, q.click_at, q.logout_at,
                               q.last_error, m.username, m.first_name, m.last_name
                        FROM member_experience_queue q
                        JOIN members m ON m.id=q.member_id
                        WHERE q.batch_id=?
                        ORDER BY q.position
                        """,
                        (batch["id"],),
                    ).fetchall()
                ]
                item["batches"].append(batch_data)
            professionals.append(item)
    return {"professionals": professionals}


def _experience_lock_path() -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings.logs_dir / "member_experience.lock"


def launch_batch(batch_id: int) -> int:
    lock_path = _experience_lock_path()
    lock_handle = lock_path.open("a+")
    locked = False
    spawned = False
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise RuntimeError("Já existe uma fila de Ajouter em execução.") from exc

        log_handle = (settings.logs_dir / "member_experience.log").open(
            "ab", buffering=0
        )
        try:
            command = [
                sys.executable,
                "-m",
                "scripts.run_member_experience",
                "--batch-id",
                str(batch_id),
            ]
            process = subprocess.Popen(
                command,
                cwd=str(settings.project_root),
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lock_handle.fileno(),),
            )
            spawned = True
            return process.pid
        finally:
            log_handle.close()
    finally:
        if locked and not spawned:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()
