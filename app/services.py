\
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import settings
from .database import add_event, get_connection, transaction, utc_now
from .username_generator import username_candidates
from .registration_network import vpn_worker_ready
from .registration_destinations import get_destination
from .worker_client import (
    WorkerClientError,
    worker_capabilities,
    worker_is_configured,
    worker_ready_from_capabilities,
)


def today_local_iso() -> str:
    try:
        tz = ZoneInfo(settings.registration_timezone)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def _unique_username(conn, first_name: str, last_name: str, member_id: int) -> str:
    for candidate in username_candidates(first_name, last_name, seed=member_id):
        exists = conn.execute(
            "SELECT 1 FROM members WHERE username = ? AND id <> ?",
            (candidate, member_id),
        ).fetchone()
        if not exists:
            return candidate
    raise RuntimeError(f"Não foi possível gerar username único para membro {member_id}.")


def _unique_email(conn, member_id: int) -> str:
    if not settings.email_domain:
        raise RuntimeError("EMAIL_DOMAIN não configurado no .env")

    # member_id garante identidade; token reduz previsibilidade.
    for _ in range(20):
        token = secrets.token_hex(2)
        email = f"member{member_id:04d}.{token}@{settings.email_domain}".lower()
        exists = conn.execute(
            "SELECT 1 FROM members WHERE email = ? AND id <> ?",
            (email, member_id),
        ).fetchone()
        if not exists:
            return email
    raise RuntimeError("Não foi possível gerar email único.")


def _validate_registration_route(route: str) -> str:
    normalized = str(route).strip().upper()
    if normalized not in {"DIRECT", "VPN"}:
        raise ValueError("Rota de cadastro inválida. Use DIRECT ou VPN.")
    return normalized


def _route_limit(route: str) -> int:
    return (
        settings.registration_vpn_daily_limit
        if route == "VPN"
        else settings.registration_direct_limit
    )


def batch_status():
    """Return the capacity and readiness of the three registration lanes."""
    today = today_local_iso()
    secondary_capabilities: dict = {}
    if worker_is_configured():
        try:
            secondary_capabilities = worker_capabilities()
        except WorkerClientError:
            secondary_capabilities = {}

    primary_vpn_ready = (
        settings.registration_vpn_authorized and vpn_worker_ready()
    )
    secondary_worker_ready = worker_ready_from_capabilities(secondary_capabilities)
    secondary_vpn_ready = (
        secondary_worker_ready and bool(secondary_capabilities.get("vpn_ready"))
    )

    with get_connection() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM members WHERE registration_status='PENDING'"
        ).fetchone()[0]
        last_started = conn.execute(
            "SELECT MAX(selected_at) FROM members WHERE selected_at IS NOT NULL"
        ).fetchone()[0]
        last_batch_size = conn.execute(
            "SELECT COUNT(*) FROM members WHERE selected_at=?",
            (last_started,),
        ).fetchone()[0] if last_started else 0

    primary_vpn_limit = settings.registration_primary_vpn_limit
    secondary_direct_limit = settings.registration_secondary_direct_limit
    secondary_vpn_limit = settings.registration_secondary_vpn_limit
    primary_vpn_capacity = min(pending, primary_vpn_limit) if primary_vpn_ready else 0
    secondary_direct_capacity = (
        min(pending, secondary_direct_limit) if secondary_worker_ready else 0
    )
    secondary_vpn_capacity = (
        min(pending, secondary_vpn_limit) if secondary_vpn_ready else 0
    )
    total_limit = primary_vpn_limit + secondary_direct_limit + secondary_vpn_limit
    available_capacity = min(pending, total_limit)

    return {
        "available": available_capacity > 0,
        "available_capacity": available_capacity,
        "pending": pending,
        "today": today,
        "last_batch_started_at": last_started,
        "last_batch_size": last_batch_size,
        "primary_vpn_limit": primary_vpn_limit,
        "primary_vpn_capacity": primary_vpn_capacity,
        "primary_vpn_ready": primary_vpn_ready,
        "secondary_direct_limit": secondary_direct_limit,
        "secondary_direct_capacity": secondary_direct_capacity,
        "secondary_worker_ready": secondary_worker_ready,
        "secondary_vpn_limit": secondary_vpn_limit,
        "secondary_vpn_capacity": secondary_vpn_capacity,
        "secondary_vpn_ready": secondary_vpn_ready,
        "direct_limit": secondary_direct_limit,
        "direct_remaining": secondary_direct_capacity,
        "direct_capacity": secondary_direct_capacity,
        "vpn_limit": primary_vpn_limit,
        "vpn_remaining": primary_vpn_capacity,
        "vpn_ready": primary_vpn_ready,
        "vpn_authorized": settings.registration_vpn_authorized,
        "vpn_capacity": primary_vpn_capacity,
        "total_limit": total_limit,
    }


def reserve_batch(destination: str) -> list[int]:
    """Reserve one batch for exactly one of the three configured lanes."""
    definition = get_destination(destination)
    target = definition["target"]
    network = definition["network"]

    if target == "PRIMARY":
        if not settings.registration_vpn_authorized or not vpn_worker_ready():
            raise RuntimeError("A VPN da VPS principal francesa não está pronta.")
    elif network == "VPN":
        if not worker_is_configured():
            raise RuntimeError("O worker da VPS secundária ainda não está configurado.")
        try:
            capabilities = worker_capabilities()
        except WorkerClientError as exc:
            raise RuntimeError("O worker da VPS secundária está indisponível.") from exc
        if not worker_ready_from_capabilities(capabilities) or not capabilities.get("vpn_ready"):
            raise RuntimeError("A VPN da VPS secundária não está pronta.")
    elif not worker_is_configured():
        raise RuntimeError("O worker da VPS secundária ainda não está configurado.")
    else:
        try:
            capabilities = worker_capabilities()
        except WorkerClientError as exc:
            raise RuntimeError("O worker da VPS secundária está indisponível.") from exc
        if not worker_ready_from_capabilities(capabilities):
            raise RuntimeError("O worker da VPS secundária não está pronto.")

    limit = max(0, int(getattr(settings, definition["limit_key"])))
    if limit <= 0:
        return []

    now = utc_now()
    today = today_local_iso()

    with transaction(immediate=True) as conn:
        rows = conn.execute(
            "SELECT * FROM members WHERE registration_status='PENDING' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        ids = []

        for row in rows:
            username = row["username"] or _unique_username(
                conn, row["first_name"], row["last_name"], row["id"]
            )
            email = row["email"] or _unique_email(conn, row["id"])
            conn.execute(
                """
                UPDATE members
                SET username=?, email=?, registration_status='SELECTED',
                    selected_date=?, selected_at=?, registration_target=?,
                    registration_route=?, registration_worker_job_id=NULL,
                    registration_worker_action=NULL,
                    registration_egress_ip=NULL,
                    registration_attempts=registration_attempts+1,
                    last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (username, email, today, now, target, network, now, row["id"]),
            )
            add_event(
                conn,
                row["id"],
                "MEMBER_SELECTED",
                f"Selecionado para {destination}: {target} / {network}.",
            )
            ids.append(row["id"])

        return ids


def get_registration_route_groups(member_ids: Iterable[int]) -> dict[str, list[int]]:
    ids = list(dict.fromkeys(int(x) for x in member_ids))
    groups = {"DIRECT": [], "VPN": []}
    if not ids:
        return groups
    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, COALESCE(registration_route, 'DIRECT') AS route "
            f"FROM members WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
    for row in rows:
        route = row["route"] if row["route"] in groups else "DIRECT"
        groups[route].append(row["id"])
    return groups


def prepare_members_by_ids(member_ids: Iterable[int]) -> list[int]:
    """
    Prepara IDs explicitamente informados para cadastro.

    Gera username/email usando exatamente a mesma lógica já existente
    no projeto e impede que um membro chegue ao navegador com campos
    obrigatórios vazios.

    Permitido somente para:
      - PENDING
      - MANUAL_INTERVENTION
      - SELECTED

    Contas já criadas ou em etapas posteriores são recusadas.
    """
    member_ids = list(dict.fromkeys(int(x) for x in member_ids))

    if not member_ids:
        return []

    now = utc_now()
    today = today_local_iso()
    prepared = []

    with transaction(immediate=True) as conn:
        for member_id in member_ids:
            row = conn.execute(
                "SELECT * FROM members WHERE id = ?",
                (member_id,),
            ).fetchone()

            if not row:
                raise RuntimeError(f"Membro {member_id} não encontrado.")

            status = row["registration_status"]

            if status not in {
                "PENDING",
                "MANUAL_INTERVENTION",
                "SELECTED",
            }:
                raise RuntimeError(
                    f"Membro {member_id}: status {status!r} não permite "
                    "novo cadastro automático."
                )

            username = row["username"] or _unique_username(
                conn,
                row["first_name"],
                row["last_name"],
                row["id"],
            )

            email = row["email"] or _unique_email(
                conn,
                row["id"],
            )

            if not username or not str(username).strip():
                raise RuntimeError(
                    f"Membro {member_id}: username não pôde ser gerado."
                )

            if not email or not str(email).strip():
                raise RuntimeError(
                    f"Membro {member_id}: email não pôde ser gerado."
                )

            conn.execute(
                """
                UPDATE members
                SET username = ?,
                    email = ?,
                    registration_status = 'SELECTED',
                    selected_date = ?,
                    selected_at = ?,
                    registration_route = COALESCE(registration_route, 'DIRECT'),
                    registration_attempts =
                        CASE
                            WHEN registration_status = 'SELECTED'
                                THEN registration_attempts
                            ELSE registration_attempts + 1
                        END,
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    username,
                    email,
                    today,
                    now,
                    now,
                    member_id,
                ),
            )

            add_event(
                conn,
                member_id,
                "MEMBER_SELECTED",
                "Preparado explicitamente para cadastro automático.",
            )

            prepared.append(member_id)

    return prepared


def get_members_by_ids(member_ids: Iterable[int]):
    member_ids = list(member_ids)
    if not member_ids:
        return []
    placeholders = ",".join("?" for _ in member_ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM members WHERE id IN ({placeholders}) ORDER BY id",
            member_ids,
        ).fetchall()
        return [dict(r) for r in rows]


def get_remote_worker_jobs(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM members
            WHERE registration_target='SECONDARY'
              AND registration_worker_job_id IS NOT NULL
              AND registration_worker_action IS NOT NULL
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def record_worker_job(member_id: int, job_id: str, action: str) -> None:
    if action not in {"create_account", "activate_profile"}:
        raise ValueError("Ação de worker inválida.")
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT registration_target, registration_status FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["registration_target"] != "SECONDARY":
            raise ValueError("O trabalho remoto exige a VPS secundária.")
        status = "FORM_PREPARING" if action == "create_account" else row["registration_status"]
        conn.execute(
            """
            UPDATE members
            SET registration_worker_job_id=?,
                registration_worker_action=?,
                registration_status=?,
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (job_id, action, status, utc_now(), member_id),
        )
        add_event(conn, member_id, "WORKER_JOB_SUBMITTED", f"{action}:{job_id}")


def finish_worker_job(member_id: int, job_id: str) -> None:
    with transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE members
            SET registration_worker_action=NULL,
                updated_at=?
            WHERE id=? AND registration_worker_job_id=?
            """,
            (utc_now(), member_id, job_id),
        )


def mark_account_created_automatic(member_id: int) -> str:
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT registration_status FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["registration_status"] not in {
            "SELECTED",
            "FORM_PREPARING",
            "MANUAL_INTERVENTION",
        }:
            raise ValueError(
                f"Status atual não permite marcar conta criada remotamente: {row['registration_status']}"
            )
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='WAITING_EMAIL_CONFIRMATION',
                email_status='PENDING',
                account_created_at=?,
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now, now, member_id),
        )
        add_event(conn, member_id, "ACCOUNT_CREATED_AUTOMATIC", "Conta criada pelo worker remoto.")
        return now


def get_pending_profile_activation_ids() -> list[int]:
    """Return confirmed profiles that can safely enter the activation queue."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM members
            WHERE email_status='CONFIRMED'
              AND profile_status IN ('PENDING', 'MANUAL_INTERVENTION')
              AND registration_status IN ('EMAIL_CONFIRMED', 'PROFILE_PENDING')
            ORDER BY id
            """
        ).fetchall()
        return [int(row["id"]) for row in rows]


def set_status(member_id: int, status: str, error: str | None = None):
    with transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE members
            SET registration_status = ?,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, error, utc_now(), member_id),
        )
        add_event(conn, member_id, status, error or "")


def mark_form_prepared(member_id: int):
    with transaction(immediate=True) as conn:
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status = 'FORM_PREPARED',
                form_prepared_at = ?,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, member_id),
        )
        add_event(conn, member_id, "FORM_PREPARED", "Formulário preenchido; termos não foram aceitos.")


def mark_account_created(member_id: int):
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT registration_status FROM members WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["registration_status"] not in {"FORM_PREPARED", "MANUAL_INTERVENTION"}:
            raise ValueError(f"Status atual não permite marcar conta criada: {row['registration_status']}")
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='WAITING_EMAIL_CONFIRMATION',
                email_status='PENDING',
                account_created_at=?,
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now, now, member_id),
        )
        add_event(conn, member_id, "ACCOUNT_CREATED", "Conta marcada manualmente como criada.")


def mark_email_confirmed(member_id: int):
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT registration_status FROM members WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["registration_status"] != "WAITING_EMAIL_CONFIRMATION":
            raise ValueError(f"Status atual não permite confirmar email: {row['registration_status']}")
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='EMAIL_CONFIRMED',
                email_status='CONFIRMED',
                profile_status='PENDING',
                email_confirmed_at=?,
                updated_at=?
            WHERE id=?
            """,
            (now, now, member_id),
        )
        add_event(conn, member_id, "EMAIL_CONFIRMED", "Email confirmado manualmente pelo operador.")



def claim_profile_activation(member_id: int) -> None:
    """Atomically claim one confirmed account for profile completion."""
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT registration_status, email_status, profile_status FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["email_status"] != "CONFIRMED":
            raise ValueError("O email precisa estar confirmado antes de ativar o perfil.")
        if row["profile_status"] == "COMPLETE" or row["registration_status"] == "PROFILE_COMPLETE":
            raise ValueError("Perfil já está completo.")
        if row["profile_status"] == "ACTIVATING":
            raise ValueError("O perfil já está sendo ativado.")
        if row["registration_status"] not in {"EMAIL_CONFIRMED", "PROFILE_PENDING"}:
            raise ValueError(
                f"Status atual não permite ativação do perfil: {row['registration_status']}"
            )

        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET profile_status='ACTIVATING',
                registration_status='PROFILE_PENDING',
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now, member_id),
        )
        add_event(
            conn, member_id, "PROFILE_ACTIVATION_STARTED",
            "Ativação automática do perfil iniciada: Je suis=homme e Ville=cidade do membro."
        )


def mark_profile_complete(member_id: int) -> None:
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT id FROM members WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='PROFILE_COMPLETE',
                profile_status='COMPLETE',
                profile_completed_at=?,
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now, now, member_id),
        )
        add_event(
            conn, member_id, "PROFILE_COMPLETE",
            "Perfil validado após salvar Je suis=homme e Ville."
        )


def mark_profile_intervention(member_id: int, error: str) -> None:
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT id FROM members WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='EMAIL_CONFIRMED',
                profile_status='MANUAL_INTERVENTION',
                last_error=?,
                updated_at=?
            WHERE id=?
            """,
            (error, now, member_id),
        )
        add_event(conn, member_id, "PROFILE_MANUAL_INTERVENTION", error)

def defer_registration_rate_limit(member_id: int, reason: str) -> None:
    """Retorna um cadastro limitado pelo site para PENDING, preservando o histórico.

    selected_at e registration_route permanecem como histórico da tentativa;
    reserve_batch não usa esses campos para bloquear novo lote no Manager.
    """
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT registration_status FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["registration_status"] in {"WAITING_EMAIL_CONFIRMATION", "EMAIL_CONFIRMED", "PROFILE_COMPLETE"}:
            raise ValueError("Conta já avançou demais para ser adiada.")
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='PENDING',
                registration_target=NULL,
                registration_worker_job_id=NULL,
                registration_worker_action=NULL,
                form_prepared_at=NULL,
                last_error=?,
                updated_at=?
            WHERE id=?
            """,
            (reason, now, member_id),
        )
        add_event(conn, member_id, "REGISTRATION_RATE_LIMITED", reason)


def reset_to_pending(member_id: int):
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT registration_status FROM members WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["registration_status"] in {"WAITING_EMAIL_CONFIRMATION", "EMAIL_CONFIRMED", "PROFILE_COMPLETE"}:
            raise ValueError("Conta já avançou demais para voltar a PENDING.")
        now = utc_now()
        conn.execute(
            """
            UPDATE members
            SET registration_status='PENDING',
                selected_date=NULL,
                selected_at=NULL,
                registration_target=NULL,
                registration_route=NULL,
                registration_worker_job_id=NULL,
                registration_worker_action=NULL,
                registration_egress_ip=NULL,
                form_prepared_at=NULL,
                last_error=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now, member_id),
        )
        add_event(conn, member_id, "RESET_PENDING", "Registro devolvido manualmente para PENDING.")


def comment_member_lists() -> dict[str, list[dict]]:
    """Return members available for comments and those marked dissatisfied."""
    with get_connection() as conn:
        eligible = [
            dict(row)
            for row in conn.execute(
                """SELECT m.id,m.first_name,m.last_name,m.username,m.profile_status,
                          m.comment_eligibility,m.dissatisfied_at,m.dissatisfied_reason
                   FROM members m
                   WHERE m.profile_status='COMPLETE'
                     AND COALESCE(m.comment_eligibility,'ELIGIBLE')='ELIGIBLE'
                     AND NOT EXISTS (
                         SELECT 1 FROM comment_member_usage u
                         WHERE u.member_id=m.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM comment_bank c
                         WHERE c.author_member_id=m.id
                     )
                   ORDER BY m.id"""
            ).fetchall()
        ]
        dissatisfied = [
            dict(row)
            for row in conn.execute(
                """SELECT m.id,m.first_name,m.last_name,m.username,m.profile_status,
                          m.comment_eligibility,m.dissatisfied_at,m.dissatisfied_reason
                   FROM members m
                   WHERE m.comment_eligibility='DISSATISFIED'
                   ORDER BY m.id"""
            ).fetchall()
        ]
    return {"eligible": eligible, "dissatisfied": dissatisfied}


def mark_member_dissatisfied(member_id: int, reason: str | None = None) -> None:
    clean_reason = (reason or '').strip() or None
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT id,comment_eligibility FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["comment_eligibility"] == "DISSATISFIED":
            raise ValueError("Membro já está marcado como insatisfeito.")

        positive = conn.execute(
            """SELECT 1
               FROM comment_bank
               WHERE author_member_id=?
                 AND COALESCE(feedback_kind,'POSITIVE')='POSITIVE'
               LIMIT 1""",
            (member_id,),
        ).fetchone()
        if positive:
            raise ValueError(
                "Este membro já está associado a um comentário positivo e não pode ser marcado como insatisfeito."
            )

        assigned = conn.execute(
            "SELECT 1 FROM comment_bank WHERE author_member_id=? LIMIT 1",
            (member_id,),
        ).fetchone()
        used = conn.execute(
            "SELECT 1 FROM comment_member_usage WHERE member_id=? LIMIT 1",
            (member_id,),
        ).fetchone()
        if assigned or used:
            raise ValueError(
                "Este membro já está associado a um comentário e não pode ser separado agora."
            )

        now = utc_now()
        conn.execute(
            """UPDATE members
               SET comment_eligibility='DISSATISFIED',
                   dissatisfied_at=?,
                   dissatisfied_reason=?,
                   updated_at=?
               WHERE id=?""",
            (now, clean_reason, now, member_id),
        )
        add_event(
            conn,
            member_id,
            "MEMBER_MARKED_DISSATISFIED",
            clean_reason or "Membro separado por experiência insatisfatória.",
        )


def unmark_member_dissatisfied(member_id: int) -> None:
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT id,comment_eligibility FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not row:
            raise ValueError("Membro inexistente.")
        if row["comment_eligibility"] != "DISSATISFIED":
            raise ValueError("Membro não está marcado como insatisfeito.")

        now = utc_now()
        conn.execute(
            """UPDATE members
               SET comment_eligibility='ELIGIBLE',
                   dissatisfied_at=NULL,
                   dissatisfied_reason=NULL,
                   updated_at=?
               WHERE id=?""",
            (now, member_id),
        )
        add_event(
            conn,
            member_id,
            "MEMBER_MARKED_ELIGIBLE",
            "Membro retornou à fila elegível para comentários.",
        )


def dashboard_data():
    status = batch_status()

    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM members"
        ).fetchone()[0]

        def count_status(value):
            return conn.execute(
                """
                SELECT COUNT(*)
                FROM members
                WHERE registration_status=?
                """,
                (value,),
            ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT *
            FROM members
            ORDER BY
                CASE registration_status
                    WHEN 'FORM_PREPARED' THEN 1
                    WHEN 'WAITING_EMAIL_CONFIRMATION' THEN 2
                    WHEN 'PROFILE_PENDING' THEN 3
                    WHEN 'EMAIL_CONFIRMED' THEN 4
                    WHEN 'PROFILE_COMPLETE' THEN 5
                    WHEN 'MANUAL_INTERVENTION' THEN 6
                    WHEN 'SELECTED' THEN 7
                    WHEN 'FORM_PREPARING' THEN 8
                    ELSE 7
                END,
                id
            LIMIT 250
            """
        ).fetchall()

        return {
            "total": total,
            "pending": count_status("PENDING"),

            "limit": status["total_limit"],
            "registration_day": status["today"],

            "batch_available": status["available"],
            "available_capacity": status["available_capacity"],
            "last_batch_size": status["last_batch_size"],

            "direct_limit": status["direct_limit"],
            "direct_remaining": status["direct_remaining"],
            "direct_capacity": status["direct_capacity"],
            "vpn_limit": status["vpn_limit"],
            "vpn_remaining": status["vpn_remaining"],
            "vpn_ready": status["vpn_ready"],
            "vpn_authorized": status["vpn_authorized"],
            "vpn_capacity": status["vpn_capacity"],

            "prepared": count_status("FORM_PREPARED"),
            "waiting_email": count_status(
                "WAITING_EMAIL_CONFIRMATION"
            ),
            "email_confirmed": count_status(
                "EMAIL_CONFIRMED"
            ),
            "manual": conn.execute(
                """
                SELECT COUNT(*) FROM members
                WHERE registration_status='MANUAL_INTERVENTION'
                   OR profile_status='MANUAL_INTERVENTION'
                """
            ).fetchone()[0],

            "profile_pending": conn.execute(
                """
                SELECT COUNT(*)
                FROM members
                WHERE email_status='CONFIRMED'
                  AND profile_status IN ('PENDING', 'ACTIVATING', 'MANUAL_INTERVENTION')
                """
            ).fetchone()[0],

            "primary_vpn_limit": status["primary_vpn_limit"],
            "primary_vpn_capacity": status["primary_vpn_capacity"],
            "primary_vpn_ready": status["primary_vpn_ready"],
            "secondary_direct_limit": status["secondary_direct_limit"],
            "secondary_direct_capacity": status["secondary_direct_capacity"],
            "secondary_worker_ready": status["secondary_worker_ready"],
            "secondary_vpn_limit": status["secondary_vpn_limit"],
            "secondary_vpn_capacity": status["secondary_vpn_capacity"],
            "secondary_vpn_ready": status["secondary_vpn_ready"],

            "profile_complete": conn.execute(
                """
                SELECT COUNT(*)
                FROM members
                WHERE profile_status='COMPLETE'
                """
            ).fetchone()[0],

            "members": [dict(r) for r in rows],
        }

