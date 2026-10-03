from __future__ import annotations

import asyncio
import logging
import re
import shutil
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import async_playwright

from app.config import settings
from app.database import get_connection, transaction, utc_now
from app.site_adapter.meslibertines import MesLibertinesAdapter

log = logging.getLogger("comment_scout")

ACTIVE = "ACTIVE"
FOUND = "FOUND"
EXPIRED = "EXPIRED"
ERROR = "ERROR"

RESULT_FOUND = "FOUND"
RESULT_NOT_FOUND = "NOT_FOUND"
RESULT_ERROR = "ERROR"

PROTECTION_MARKERS = (
    "cloudflare",
    "checking your browser",
    "verify you are human",
    "captcha",
    "too many requests",
    "rate limit",
    "access denied",
)

SPECIAL_COMMENT_PROFESSIONALS = frozenset({"brenda", "mariela", "brenda 2"})


def is_special_comment_professional(name: str | None) -> bool:
    return (name or "").strip().casefold() in SPECIAL_COMMENT_PROFESSIONALS


def scout_check_offsets_for_professional(name: str | None) -> tuple[int, ...]:
    if is_special_comment_professional(name):
        return (420, 480)
    return settings.scout_check_offsets_minutes


def _parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    """Normalize visible text for resilient exact-content matching."""
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def comment_matches(page_text: str, expected_comment: str) -> bool:
    needle = normalize_text(expected_comment)
    haystack = normalize_text(page_text)
    return bool(needle) and needle in haystack


def _first_check_at(
    published_at: datetime,
    professional_name: str | None = None,
) -> datetime:
    offsets = scout_check_offsets_for_professional(professional_name)
    return published_at + timedelta(minutes=offsets[0])


def _next_check_at(
    published_at: datetime,
    completed_checks: int,
    now: datetime,
    professional_name: str | None = None,
) -> datetime:
    """Use the configured publication offsets for the specific professional."""
    offsets = scout_check_offsets_for_professional(professional_name)
    if not 0 <= completed_checks < len(offsets):
        raise ValueError("Não há outra verificação prevista para este Scout.")
    return published_at + timedelta(minutes=offsets[completed_checks])


def retry_after_comment_submission(published_at: str) -> str:
    """Return the earliest retry time: 24h after the original submission."""
    return _iso(_parse_timestamp(published_at) + timedelta(hours=24))


def create_comment_scout(
    member_id: int,
    advertisement_title: str,
    target_url: str,
    expected_comment: str,
    *,
    published_at: str | None = None,
) -> int:
    """
    Create a Scout after a comment has actually been published.

    The future comment-publisher should call this only after its publish action
    succeeds. The Scout itself never publishes comments; it only verifies that
    the already-published text becomes visible on the target advertisement.
    """
    advertisement_title = (advertisement_title or "").strip()
    target_url = (target_url or "").strip()
    expected_comment = (expected_comment or "").strip()

    if not advertisement_title:
        raise ValueError("advertisement_title é obrigatório para criar o Scout.")
    if not target_url.startswith(("http://", "https://")):
        raise ValueError("target_url deve ser uma URL http/https válida.")
    if not expected_comment:
        raise ValueError("expected_comment é obrigatório para criar o Scout.")

    published_dt = _parse_timestamp(published_at) if published_at else datetime.now(timezone.utc)
    created = utc_now()
    next_check = _iso(_first_check_at(published_dt, advertisement_title))

    with transaction(immediate=True) as conn:
        member = conn.execute(
            "SELECT id FROM members WHERE id=?",
            (member_id,),
        ).fetchone()
        if not member:
            raise ValueError("Membro inexistente.")

        # Idempotency guard. A publisher retry must not create duplicate Scouts
        # for the same member/comment/advertisement publication.
        duplicate = conn.execute(
            """
            SELECT id
            FROM comment_scouts
            WHERE member_id=?
              AND advertisement_title=?
              AND target_url=?
              AND expected_comment=?
              AND status='ACTIVE'
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                member_id,
                advertisement_title,
                target_url,
                expected_comment,
            ),
        ).fetchone()
        if duplicate:
            return int(duplicate["id"])

        cursor = conn.execute(
            """
            INSERT INTO comment_scouts(
                member_id,
                advertisement_title,
                target_url,
                expected_comment,
                status,
                published_at,
                checks_done,
                max_checks,
                next_check_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'ACTIVE', ?, 0, ?, ?, ?, ?)
            """,
            (
                member_id,
                advertisement_title,
                target_url,
                expected_comment,
                _iso(published_dt),
                settings.scout_max_checks,
                next_check,
                created,
                created,
            ),
        )
        scout_id = int(cursor.lastrowid)

        conn.execute(
            """
            INSERT INTO member_events(member_id, event_type, message, created_at)
            VALUES (?, 'COMMENT_SCOUT_CREATED', ?, ?)
            """,
            (
                member_id,
                f"Scout #{scout_id} criado para o anúncio: {advertisement_title}",
                created,
            ),
        )

    log.info(
        "Scout %s criado para member_id=%s; primeira checagem em %s",
        scout_id,
        member_id,
        next_check,
    )
    return scout_id


def record_comment_published(
    member_id: int,
    advertisement_title: str,
    target_url: str,
    comment_text: str,
    *,
    published_at: str | None = None,
) -> int:
    """Integration hook to call immediately after a successful comment post."""
    return create_comment_scout(
        member_id,
        advertisement_title,
        target_url,
        comment_text,
        published_at=published_at,
    )


def _due_scouts(limit: int = 10) -> list[dict[str, Any]]:
    now = utc_now()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                s.*,
                m.first_name,
                m.last_name,
                m.username,
                m.birth_date
            FROM comment_scouts s
            JOIN members m ON m.id=s.member_id
            WHERE s.status='ACTIVE'
              AND s.next_check_at IS NOT NULL
              AND s.next_check_at <= ?
            ORDER BY s.next_check_at, s.id
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return [dict(row) for row in rows]


async def _verify_visible_comment(scout: dict[str, Any]) -> tuple[str, str]:
    """Return (result, details). No security control is bypassed."""
    async with async_playwright() as p:
        launch_kwargs = {"headless": settings.scout_headless}
        system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if system_chromium:
            launch_kwargs["executable_path"] = system_chromium
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(
                scout["target_url"],
                wait_until="domcontentloaded",
                timeout=settings.scout_page_timeout_seconds * 1000,
            )
            await page.wait_for_timeout(1000)

            # If the ordinary +18 gate is shown, complete it using the same
            # member birth date already used by the authorized registration.
            adapter = MesLibertinesAdapter()
            try:
                await adapter.accept_initial_age_gate(page, scout["birth_date"])
            except Exception as exc:
                return RESULT_ERROR, f"Gate +18 não pôde ser concluído: {type(exc).__name__}: {exc}"

            # Reload target once after a gate because some site variants return
            # to the homepage after accepting it.
            if scout["target_url"] not in page.url:
                await page.goto(
                    scout["target_url"],
                    wait_until="domcontentloaded",
                    timeout=settings.scout_page_timeout_seconds * 1000,
                )
                await page.wait_for_timeout(1000)

            try:
                body = await page.locator("body").inner_text(timeout=10_000)
            except Exception as exc:
                return RESULT_ERROR, f"Não foi possível ler a página: {type(exc).__name__}: {exc}"

            lowered = normalize_text(body)
            if any(marker in lowered for marker in PROTECTION_MARKERS):
                return RESULT_ERROR, "Página apresentou CAPTCHA, Cloudflare, rate limit ou bloqueio."

            if comment_matches(body, scout["expected_comment"]):
                return RESULT_FOUND, "Texto esperado encontrado na página."

            # One passive scroll catches lazy-loaded comment blocks without
            # clicking, refreshing repeatedly, or generating engagement.
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
                body_after_scroll = await page.locator("body").inner_text(timeout=10_000)
                if comment_matches(body_after_scroll, scout["expected_comment"]):
                    return RESULT_FOUND, "Texto esperado encontrado após carregar o final da página."
            except Exception:
                pass

            return RESULT_NOT_FOUND, "Texto esperado ainda não está visível na página."

        finally:
            await context.close()
            await browser.close()


def _store_check_result(scout: dict[str, Any], result: str, details: str) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now = _iso(now_dt)
    attempt = int(scout["checks_done"]) + 1
    max_checks = int(scout["max_checks"])

    with transaction(immediate=True) as conn:
        current = conn.execute(
            "SELECT status, checks_done FROM comment_scouts WHERE id=?",
            (scout["id"],),
        ).fetchone()
        if not current or current["status"] != ACTIVE:
            return {"status": "SKIPPED", "attempt": attempt}
        if int(current["checks_done"]) != int(scout["checks_done"]):
            return {"status": "SKIPPED", "attempt": attempt}

        conn.execute(
            """
            INSERT INTO comment_scout_checks(
                scout_id, attempt_no, checked_at, result, details
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (scout["id"], attempt, now, result, details[:1000]),
        )

        if result == RESULT_FOUND:
            new_status = FOUND
            next_check = None
            conn.execute(
                """
                UPDATE comment_scouts
                SET status='FOUND', checks_done=?, last_checked_at=?,
                    found_at=?, next_check_at=NULL, last_error=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (attempt, now, now, now, scout["id"]),
            )
        elif attempt >= max_checks:
            if result == RESULT_ERROR:
                successful_checks = conn.execute(
                    """
                    SELECT COUNT(*) FROM comment_scout_checks
                    WHERE scout_id=? AND result IN ('FOUND','NOT_FOUND')
                    """,
                    (scout["id"],),
                ).fetchone()[0]
                if successful_checks == 0:
                    new_status = ERROR
                    next_check = None
                    conn.execute(
                        """
                        UPDATE comment_scouts
                        SET status='ERROR', checks_done=?, last_checked_at=?,
                            next_check_at=NULL, last_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (attempt, now, details[:1000], now, scout["id"]),
                    )
                else:
                    new_status = EXPIRED
                    next_check = None
                    conn.execute(
                        """
                        UPDATE comment_scouts
                        SET status='EXPIRED', checks_done=?, last_checked_at=?,
                            expired_at=?, next_check_at=NULL, last_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (attempt, now, now, details[:1000], now, scout["id"]),
                    )
            else:
                new_status = EXPIRED
                next_check = None
                conn.execute(
                    """
                    UPDATE comment_scouts
                    SET status='EXPIRED', checks_done=?, last_checked_at=?,
                        expired_at=?, next_check_at=NULL, last_error=NULL,
                        updated_at=?
                    WHERE id=?
                    """,
                    (attempt, now, now, now, scout["id"]),
                )
        else:
            new_status = ACTIVE
            published_dt = _parse_timestamp(scout["published_at"])
            next_dt = _next_check_at(
                published_dt,
                attempt,
                now_dt,
                scout["advertisement_title"],
            )
            next_check = _iso(next_dt)
            conn.execute(
                """
                UPDATE comment_scouts
                SET checks_done=?, last_checked_at=?, next_check_at=?,
                    last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    attempt,
                    now,
                    next_check,
                    details[:1000] if result == RESULT_ERROR else None,
                    now,
                    scout["id"],
                ),
            )

    # Keep the automated-comment bank synchronized with the Scout outcome.
    # A confirmed NOT_FOUND on the final check recycles the genuine feedback,
    # but the member account remains consumed in comment_member_usage so it
    # can never be reused for another automated comment. Technical failures
    # are intentionally NOT recycled because publication would be uncertain.
    recycled = False
    next_comment_scheduled = None
    try:
        with transaction(immediate=True) as conn:
            if new_status == FOUND:
                conn.execute(
                    "UPDATE comment_bank SET status='USED', published_at=?, last_error=NULL, updated_at=? WHERE scout_id=?",
                    (now, now, scout["id"]),
                )
                conn.execute(
                    "UPDATE comment_member_usage SET outcome='CONFIRMED' WHERE comment_id IN (SELECT id FROM comment_bank WHERE scout_id=?)",
                    (scout["id"],),
                )
                if attempt == 1:
                    # Only a first-check FOUND releases the next confirmed
                    # queue item one hour later. A second-check FOUND keeps
                    # the normal professional cadence.
                    from app.comment_automation.service import (
                        schedule_next_comment_after_scout_found,
                    )
                    professional = conn.execute(
                        """
                        SELECT c.professional_id, p.name AS professional_name
                        FROM comment_bank c
                        JOIN comment_professionals p ON p.id=c.professional_id
                        WHERE c.scout_id=?
                        LIMIT 1
                        """,
                        (scout["id"],),
                    ).fetchone()
                    if professional:
                        next_comment_scheduled = schedule_next_comment_after_scout_found(
                            conn,
                            professional_id=int(professional["professional_id"]),
                            professional_name=professional["professional_name"],
                            found_at=now,
                        )
            elif new_status == EXPIRED and result == RESULT_NOT_FOUND:
                retry_at = retry_after_comment_submission(scout["published_at"])
                professional = None
                if is_special_comment_professional(scout["advertisement_title"]):
                    professional = conn.execute(
                        """
                        SELECT c.id AS comment_id, c.professional_id
                        FROM comment_bank c
                        WHERE c.scout_id=?
                        LIMIT 1
                        """,
                        (scout["id"],),
                    ).fetchone()
                cur = conn.execute(
                    """UPDATE comment_bank
                       SET status='AVAILABLE', schedule_date=NULL,
                           scheduled_for=?, submitted_at=NULL, published_at=NULL, scout_id=NULL,
                           last_error=?, updated_at=?
                       WHERE scout_id=?""",
                    (retry_at, f"Scout #{scout['id']}: não encontrado após {attempt}/{max_checks} verificações; feedback liberado novamente após cooldown de 24 horas.", now, scout["id"]),
                )
                recycled = cur.rowcount > 0
                if professional:
                    from app.comment_automation.service import (
                        schedule_next_comment_after_scout_expired,
                    )
                    next_comment_scheduled = schedule_next_comment_after_scout_expired(
                        conn,
                        professional_id=int(professional["professional_id"]),
                        now_utc=now,
                        exclude_comment_id=int(professional["comment_id"]),
                    )
            elif new_status == ERROR:
                conn.execute(
                    "UPDATE comment_bank SET status='AWAITING_CONFIRMATION', last_error=?, updated_at=? WHERE scout_id=?",
                    (details[:1000], now, scout["id"]),
                )
    except Exception:
        log.exception("Scout %s: falha ao sincronizar banco de comentários.", scout["id"])

    return {
        "status": new_status,
        "attempt": attempt,
        "max_checks": max_checks,
        "next_check_at": next_check,
        "result": result,
        "recycled": recycled,
        "next_comment_scheduled": next_comment_scheduled,
    }


async def _notify_terminal_result(scout: dict[str, Any], outcome: dict[str, Any]) -> None:
    if outcome["status"] not in {FOUND, EXPIRED, ERROR}:
        return

    # Delayed import avoids coupling the Scout scheduler to the batch notifier.
    from app.discord_notifier import send_discord

    member_name = f"{scout['first_name']} {scout['last_name']}"
    account = scout.get("username") or "—"
    attempt = outcome["attempt"]
    max_checks = outcome["max_checks"]

    if outcome["status"] == FOUND:
        message = (
            "✅ **SCOUT — COMENTÁRIO ENCONTRADO**\n"
            f"Membro: **{member_name}** (#{scout['member_id']:03d})\n"
            f"Conta: `{account}`\n"
            f"Anúncio: **{scout['advertisement_title']}**\n"
            f"Verificação: **{attempt}/{max_checks}**\n"
            "O Scout foi encerrado automaticamente."
        )
    elif outcome["status"] == EXPIRED:
        message = (
            "⚠️ **SCOUT — COMENTÁRIO NÃO ENCONTRADO**\n"
            f"Membro: **{member_name}** (#{scout['member_id']:03d})\n"
            f"Conta: `{account}`\n"
            f"Anúncio: **{scout['advertisement_title']}**\n"
            f"Foram concluídas **{attempt}/{max_checks} verificações** sem confirmação.\n"
            "Status: **EXPIRADO / NÃO_ENCONTRADO**.\n"
            + ("O feedback voltou para a fila e poderá ser reenviado após o cooldown de **24 horas a partir do envio original**." if outcome.get("recycled") else "")
        )
    else:
        message = (
            "🚨 **SCOUT — VERIFICAÇÃO INCONCLUSIVA**\n"
            f"Membro: **{member_name}** (#{scout['member_id']:03d})\n"
            f"Conta: `{account}`\n"
            f"Anúncio: **{scout['advertisement_title']}**\n"
            f"As **{attempt}/{max_checks} verificações** terminaram com erro técnico.\n"
            "O Scout foi encerrado para revisão manual."
        )

    try:
        await send_discord(message)
    except Exception:
        log.exception("Scout %s: falha ao enviar notificação Discord.", scout["id"])


async def run_due_scout_checks() -> int:
    due = _due_scouts(limit=settings.scout_max_due_per_loop)
    if not due:
        return 0

    processed = 0
    for scout in due:
        try:
            result, details = await _verify_visible_comment(scout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Scout %s: erro inesperado na verificação", scout["id"])
            result = RESULT_ERROR
            details = f"{type(exc).__name__}: {exc}"

        outcome = _store_check_result(scout, result, details)
        processed += 1

        log.info(
            "Scout %s tentativa %s/%s: %s -> %s",
            scout["id"],
            outcome.get("attempt"),
            outcome.get("max_checks", scout["max_checks"]),
            result,
            outcome.get("status"),
        )

        # Notify Discord for every verification, not only terminal outcomes.
        if outcome.get("status") != "SKIPPED":
            try:
                from app.discord_notifier import send_discord
                member_name = f"{scout['first_name']} {scout['last_name']}"
                result_label = {RESULT_FOUND: "ENCONTRADO", RESULT_NOT_FOUND: "NÃO ENCONTRADO", RESULT_ERROR: "ERRO TÉCNICO"}.get(result, result)
                await send_discord(
                    f"🔎 **SCOUT — VERIFICAÇÃO {outcome['attempt']}/{outcome['max_checks']}**\n"
                    f"Membro: **{member_name}** (#{scout['member_id']:03d})\n"
                    f"Anúncio: **{scout['advertisement_title']}**\n"
                    f"Resultado: **{result_label}**"
                )
            except Exception:
                log.exception("Scout %s: falha ao notificar verificação no Discord.", scout["id"])

        # Keep the richer terminal message as a separate closure notification.
        await _notify_terminal_result(scout, outcome)

    return processed


async def scout_watch_loop() -> None:
    """Persistent scheduler: checks each Scout at publication +6h and +8h."""
    while True:
        try:
            await run_due_scout_checks()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erro no loop do Scout de comentários.")

        await asyncio.sleep(settings.scout_scheduler_poll_seconds)


def scout_member_options() -> list[dict[str, Any]]:
    """Accounts that are far enough in the flow to be associated with a comment."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, first_name, last_name, username, registration_status
            FROM members
            WHERE username IS NOT NULL
              AND registration_status IN (
                  'WAITING_EMAIL_CONFIRMATION',
                  'EMAIL_CONFIRMED',
                  'PROFILE_COMPLETE'
              )
            ORDER BY id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def scout_dashboard_data(limit: int = 30) -> dict[str, Any]:
    with get_connection() as conn:
        def count(status: str) -> int:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM comment_scouts WHERE status=?",
                    (status,),
                ).fetchone()[0]
            )

        rows = conn.execute(
            """
            SELECT
                s.id,
                s.member_id,
                s.advertisement_title,
                s.target_url,
                s.expected_comment,
                s.status,
                s.published_at,
                s.checks_done,
                s.max_checks,
                s.next_check_at,
                s.last_checked_at,
                s.found_at,
                s.expired_at,
                s.last_error,
                m.first_name,
                m.last_name,
                m.username
            FROM comment_scouts s
            JOIN members m ON m.id=s.member_id
            ORDER BY
                CASE s.status
                    WHEN 'ACTIVE' THEN 1
                    WHEN 'FOUND' THEN 2
                    WHEN 'EXPIRED' THEN 3
                    ELSE 4
                END,
                COALESCE(s.next_check_at, s.updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return {
            "active": count(ACTIVE),
            "found": count(FOUND),
            "expired": count(EXPIRED),
            "error": count(ERROR),
            "rows": [dict(row) for row in rows],
        }
