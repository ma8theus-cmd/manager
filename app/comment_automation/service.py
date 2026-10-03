from __future__ import annotations
import asyncio, json, logging, shutil, re, html, urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from playwright.async_api import async_playwright
from app.config import settings
from app.database import get_connection, transaction, utc_now
from app.discord_notifier import send_discord
from app.comment_automation.manual_cloudflare import is_cloudflare_intervention
from app.comment_automation.discord_manual_gate import wait_for_manual_cloudflare
from app.comment_automation.twocaptcha import install_turnstile_interceptor
from app.scout import record_comment_published
from app.scout.service import is_special_comment_professional
from app.site_adapter.meslibertines import MesLibertinesAdapter
from app.comment_automation.feedback import (
    FEEDBACK_NEGATIVE,
    FEEDBACK_POSITIVE,
    filter_feedback_rows,
    normalize_feedback_kind,
)
from app.comment_automation.importer import UNASSIGNED_NEGATIVE_PROFESSIONAL, is_comment_eligible

log = logging.getLogger('comment_automation')
PROFESSIONALS = ('Amanda','Paola','Karla','Agnes')

def seed_comment_bank() -> None:
    seed_path = settings.project_root / 'data' / 'comment_bank_seed.json'
    if not seed_path.exists(): return
    rows = json.loads(seed_path.read_text(encoding='utf-8'))
    now = utc_now()
    with transaction(immediate=True) as conn:
        for name in PROFESSIONALS:
            conn.execute(
                "INSERT OR IGNORE INTO comment_professionals(name,created_at,updated_at,feedback_kind) VALUES (?,?,?,?)",
                (name, now, now, FEEDBACK_POSITIVE),
            )
        ids = {
            r['name']: r['id']
            for r in conn.execute(
                "SELECT id,name FROM comment_professionals WHERE feedback_kind=?",
                (FEEDBACK_POSITIVE,),
            ).fetchall()
        }
        for row in rows:
            conn.execute(
                '''INSERT OR IGNORE INTO comment_bank(
                       source_no,professional_id,age_band,comment_text,
                       feedback_kind,status,created_at,updated_at
                   ) VALUES (?,?,?,?,?,\'AVAILABLE\',?,?)''',
                (
                    row['source_no'],
                    ids[row['professional']],
                    row['age_band'],
                    row['text'],
                    FEEDBACK_POSITIVE,
                    now,
                    now,
                ),
            )

def save_professional_url(
    name: str,
    url: str,
    feedback_kind: str = FEEDBACK_POSITIVE,
) -> None:
    name=(name or '').strip(); url=(url or '').strip()
    kind = normalize_feedback_kind(feedback_kind)
    if not name: raise ValueError('Profissional inválida.')
    if url and not url.startswith(('http://','https://')): raise ValueError('URL inválida.')
    with transaction(immediate=True) as conn:
        row=conn.execute(
            'SELECT id FROM comment_professionals WHERE name=? AND feedback_kind=?',
            (name, kind),
        ).fetchone()
        if not row: raise ValueError('Profissional inexistente nessa aba.')
        conn.execute(
            'UPDATE comment_professionals SET target_url=?, updated_at=? WHERE id=?',
            (url or None,utc_now(),row['id']),
        )

def _infer_professional_name(url: str) -> str:
    req=urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw=resp.read(350000).decode('utf-8','replace')
    except Exception as exc:
        raise ValueError(f'Não foi possível ler o anúncio: {type(exc).__name__}.')
    candidates=[]
    for pat in [r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', r'<h1[^>]*>(.*?)</h1>', r'<title[^>]*>(.*?)</title>']:
        m=re.search(pat, raw, re.I|re.S)
        if m:
            text=html.unescape(re.sub(r'<[^>]+>',' ',m.group(1)))
            text=re.sub(r'\s+',' ',text).strip()
            if text: candidates.append(text)
    for text in candidates:
        # Prefer a leading proper-name token; MesLibertines titles commonly begin with the profile name.
        text=re.split(r'\s*[|–—-]\s*', text, maxsplit=1)[0].strip()
        m=re.match(r"^([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’]{1,30})\b", text)
        if m and m.group(1).lower() not in {'escort','massage','profil','meslibertines','annonce'}:
            return m.group(1).strip().title()
    raise ValueError('Não consegui identificar automaticamente o nome profissional a partir do anúncio.')

def add_professional_from_url(
    url: str,
    feedback_kind: str = FEEDBACK_POSITIVE,
    name: str | None = None,
) -> dict[str, object]:
    url=(url or '').strip()
    kind = normalize_feedback_kind(feedback_kind)
    if not url.startswith(('http://','https://')): raise ValueError('URL inválida.')
    name=(name or '').strip() or _infer_professional_name(url)
    if not name: raise ValueError('Nome da profissional é obrigatório.')
    now=utc_now()
    with transaction(immediate=True) as conn:
        same_url=conn.execute(
            'SELECT id,name,target_url,enabled,feedback_kind FROM comment_professionals WHERE target_url=?',
            (url,),
        ).fetchone()
        if same_url:
            if normalize_feedback_kind(same_url['feedback_kind']) != kind:
                raise ValueError('Este anúncio já está cadastrado na aba de feedbacks oposta.')
            return dict(same_url)
        existing=conn.execute(
            'SELECT id,name,target_url,enabled,feedback_kind FROM comment_professionals WHERE lower(name)=lower(?)',
            (name,),
        ).fetchone()
        if existing:
            if normalize_feedback_kind(existing['feedback_kind']) != kind:
                raise ValueError(f'Profissional {name} já pertence à aba de feedbacks oposta.')
            conn.execute(
                'UPDATE comment_professionals SET target_url=?,enabled=1,updated_at=? WHERE id=?',
                (url,now,existing['id']),
            )
            row=conn.execute(
                'SELECT id,name,target_url,enabled,feedback_kind FROM comment_professionals WHERE id=?',
                (existing['id'],),
            ).fetchone()
            return dict(row)
        cur=conn.execute(
            'INSERT INTO comment_professionals(name,target_url,enabled,feedback_kind,created_at,updated_at) VALUES (?,?,1,?,?,?)',
            (name,url,kind,now,now),
        )
        row=conn.execute(
            'SELECT id,name,target_url,enabled,feedback_kind FROM comment_professionals WHERE id=?',
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)

def comment_assignment_fields(*, member_id: int, now: str, require_confirmation: bool) -> dict[str, object]:
    if require_confirmation:
        return {
            'status': 'AWAITING_CONFIRMATION',
            'confirmation_requested_at': now,
            'scheduled_for': None,
            'confirmed_at': None,
            'confirmed_by_member_id': None,
        }
    return {
        'status': 'AVAILABLE',
        'confirmation_requested_at': None,
        'scheduled_for': now,
        'confirmed_at': now,
        'confirmed_by_member_id': member_id,
    }


def _as_utc(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec='seconds')


def _member_notification_line(row: dict[str, Any]) -> str:
    name = " ".join(
        part for part in (row.get("first_name"), row.get("last_name")) if part
    ).strip() or "Membro não identificado"
    member_id = row.get("author_member_id") or "—"
    username = row.get("username") or "—"
    return "Membro: **{}** (#{})\nUsuário: `{}`".format(name, member_id, username)


def negative_comment_retry_at(
    *,
    now_utc: str | datetime,
    previous_submissions: list[str | datetime],
) -> str | None:
    """Return the next global negative-feedback slot, one hour apart."""
    if not previous_submissions:
        return None
    now = _as_utc(now_utc)
    latest = max(_as_utc(value) for value in previous_submissions)
    retry_at = latest + timedelta(hours=1)
    return _iso(retry_at) if now < retry_at else None


def next_comment_after_scout_found(found_at: str | datetime) -> str:
    """Return the earliest next-comment time after a confirmed Scout."""
    return _iso(_as_utc(found_at) + timedelta(hours=1))


def comment_publish_block(
    *,
    professional_id: int,
    now_utc: datetime,
    previous_comments: list[dict[str, Any]],
    professional_name: str | None = None,
) -> tuple[str, str] | None:
    now_utc = _as_utc(now_utc)
    latest_submission: datetime | None = None
    latest_professional_submission: datetime | None = None
    latest_professional_found_at: datetime | None = None
    latest_professional_found_attempt: int | None = None
    latest_professional_scout_status: str | None = None
    latest_professional_name = professional_name

    for previous in previous_comments:
        submitted_at = previous.get('submitted_at')
        if not submitted_at:
            continue
        submitted = _as_utc(submitted_at)
        if latest_submission is None or submitted > latest_submission:
            latest_submission = submitted
        if (
            previous.get('professional_id') == professional_id
            and (
                latest_professional_submission is None
                or submitted > latest_professional_submission
            )
        ):
            latest_professional_submission = submitted
            found_at = previous.get('scout_found_at')
            latest_professional_found_at = _as_utc(found_at) if found_at else None
            found_attempt = previous.get('scout_found_attempt')
            latest_professional_found_attempt = int(found_attempt) if found_attempt else None
            latest_professional_scout_status = previous.get('scout_status')
            latest_professional_name = (
                previous.get('professional_name') or latest_professional_name
            )

    if (
        latest_professional_scout_status == 'EXPIRED'
        and latest_professional_found_attempt == 2
        and is_special_comment_professional(latest_professional_name)
    ):
        pass
    elif (
        latest_professional_found_at is not None
        and latest_professional_found_attempt == 1
    ):
        retry_at = latest_professional_found_at + timedelta(
            hours=24 if is_special_comment_professional(latest_professional_name) else 1
        )
        if now_utc < retry_at:
            return 'scout_found_interval', _iso(retry_at)
    elif latest_professional_submission is not None:
        retry_at = latest_professional_submission + timedelta(
            minutes=settings.comment_professional_interval_minutes
        )
        if now_utc < retry_at:
            return 'professional_interval', _iso(retry_at)

    if latest_submission is not None:
        retry_at = latest_submission + timedelta(
            minutes=settings.comment_min_interval_minutes
        )
        if now_utc < retry_at:
            return 'minimum_interval', _iso(retry_at)
    return None


def schedule_next_comment_after_scout_found(
    conn,
    *,
    professional_id: int,
    found_at: str | datetime,
    professional_name: str | None = None,
) -> str | None:
    """Schedule the next queue item after Scout FOUND."""
    delay = timedelta(
        hours=24 if is_special_comment_professional(professional_name) else 1
    )
    scheduled = _iso(_as_utc(found_at) + delay)
    now = utc_now()
    row = conn.execute(
        """
        SELECT id
        FROM comment_bank
        WHERE professional_id=?
          AND status='AVAILABLE'
          AND author_member_id IS NOT NULL
          AND confirmed_at IS NOT NULL
          AND submitted_at IS NULL
        ORDER BY
            CASE WHEN scheduled_for IS NULL THEN 1 ELSE 0 END,
            scheduled_for,
            source_no,
            id
        LIMIT 1
        """,
        (professional_id,),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        """
        UPDATE comment_bank
        SET scheduled_for=CASE
                WHEN scheduled_for IS NULL OR scheduled_for < ? THEN ?
                ELSE scheduled_for
            END,
            updated_at=?
        WHERE id=?
        """,
        (scheduled, scheduled, now, row["id"]),
    )
    return scheduled


def schedule_next_comment_after_scout_expired(
    conn,
    *,
    professional_id: int,
    now_utc: str | datetime,
    exclude_comment_id: int | None = None,
) -> str | None:
    """Make the next confirmed queue item due immediately after expiry."""
    scheduled = _iso(_as_utc(now_utc))
    row = conn.execute(
        """
        SELECT id
        FROM comment_bank
        WHERE professional_id=?
          AND status='AVAILABLE'
          AND author_member_id IS NOT NULL
          AND confirmed_at IS NOT NULL
          AND submitted_at IS NULL
          AND (? IS NULL OR id<>?)
        ORDER BY
            CASE WHEN scheduled_for IS NULL THEN 1 ELSE 0 END,
            scheduled_for,
            source_no,
            id
        LIMIT 1
        """,
        (professional_id, exclude_comment_id, exclude_comment_id),
    ).fetchone()
    if not row:
        return None
    now = utc_now()
    conn.execute(
        """
        UPDATE comment_bank
        SET scheduled_for=?, updated_at=?
        WHERE id=?
        """,
        (scheduled, now, row["id"]),
    )
    return scheduled


def reconcile_scheduled_comments(
    conn,
    *,
    now_utc: datetime | str | None = None,
) -> int:
    """Repair legacy future schedules without bypassing cadence limits.

    The removed daily planner could leave assigned comments scheduled for a
    future window. Current assignments are due immediately, subject only to
    the global and per-professional cadence rules. A schedule is changed only
    when it is more than five minutes later than the next eligible time.
    """
    now_dt = _as_utc(now_utc or utc_now())
    previous_comments = [
        dict(previous)
        for previous in conn.execute(
            """
            SELECT c.professional_id, p.name AS professional_name, c.submitted_at,
                   s.found_at AS scout_found_at,
                   s.checks_done AS scout_found_attempt,
                   s.status AS scout_status
            FROM comment_bank c
            JOIN comment_professionals p ON p.id=c.professional_id
            LEFT JOIN comment_scouts s ON s.id=c.scout_id
            WHERE c.submitted_at IS NOT NULL
            """
        ).fetchall()
    ]
    rows = conn.execute(
        """
        SELECT id, professional_id, scheduled_for
        FROM comment_bank
        WHERE status='AVAILABLE'
          AND author_member_id IS NOT NULL
          AND confirmed_at IS NOT NULL
          AND submitted_at IS NULL
          AND scheduled_for IS NOT NULL
        """
    ).fetchall()

    repaired = 0
    tolerance = timedelta(minutes=5)
    for row in rows:
        block = comment_publish_block(
            professional_id=row["professional_id"],
            professional_name=conn.execute(
                "SELECT name FROM comment_professionals WHERE id=?",
                (row["professional_id"],),
            ).fetchone()["name"],
            now_utc=now_dt,
            previous_comments=previous_comments,
        )
        target = _as_utc(block[1]) if block else now_dt
        try:
            existing = _as_utc(row["scheduled_for"])
        except (TypeError, ValueError):
            existing = now_dt + tolerance + timedelta(seconds=1)

        if existing <= target + tolerance:
            continue

        conn.execute(
            """
            UPDATE comment_bank
            SET scheduled_for=?, updated_at=?
            WHERE id=? AND status='AVAILABLE'
            """,
            (_iso(target), _iso(now_dt), row["id"]),
        )
        repaired += 1

    return repaired


def assign_comment_author(comment_id: int, member_id: int | None) -> None:
    with transaction(immediate=True) as conn:
        row=conn.execute('SELECT id,status FROM comment_bank WHERE id=?',(comment_id,)).fetchone()
        if not row: raise ValueError('Comentário inexistente.')
        if row['status'] != 'AVAILABLE': raise ValueError('Comentário já reservado/publicado.')
        if member_id is not None:
            m=conn.execute(
                "SELECT id,COALESCE(comment_eligibility,'ELIGIBLE') AS comment_eligibility "
                "FROM members WHERE id=? AND profile_status='COMPLETE'",
                (member_id,),
            ).fetchone()
            if not m: raise ValueError('Autor precisa ter perfil COMPLETE.')
            if m['comment_eligibility'] == 'DISSATISFIED':
                raise ValueError('Membro marcado como insatisfeito não pode comentar anúncios.')
            used=conn.execute('SELECT 1 FROM comment_member_usage WHERE member_id=?',(member_id,)).fetchone()
            assigned=conn.execute('SELECT id FROM comment_bank WHERE author_member_id=? AND id<>?',(member_id,comment_id)).fetchone()
            if used or assigned: raise ValueError('Esta conta de membro já foi usada ou reservada para outro comentário.')
        now=utc_now()
        if member_id is None:
            conn.execute("UPDATE comment_bank SET author_member_id=NULL,status='AVAILABLE',confirmation_requested_at=NULL,confirmed_at=NULL,confirmed_by_member_id=NULL,scheduled_for=NULL,updated_at=? WHERE id=?", (now, comment_id))
        else:
            fields = comment_assignment_fields(
                member_id=member_id,
                now=now,
                require_confirmation=settings.comment_require_author_confirmation,
            )
            conn.execute(
                """UPDATE comment_bank
                   SET author_member_id=?, status=?, confirmation_requested_at=?,
                       scheduled_for=?, confirmed_at=?, confirmed_by_member_id=?, updated_at=?
                   WHERE id=?""",
                (
                    member_id,
                    fields['status'],
                    fields['confirmation_requested_at'],
                    fields['scheduled_for'],
                    fields['confirmed_at'],
                    fields['confirmed_by_member_id'],
                    now,
                    comment_id,
                ),
            )

def create_manual_comment(
    conn,
    *,
    professional_id: int,
    member_id: int,
    comment_text: str,
    feedback_kind: str = FEEDBACK_POSITIVE,
    now: str | None = None,
) -> dict[str, object]:
    """Create a manually assigned comment without starting its automatic send."""
    kind = normalize_feedback_kind(feedback_kind)
    text = " ".join((comment_text or "").split())
    if not text:
        raise ValueError("O comentário não pode ficar vazio.")
    if not is_comment_eligible(text):
        raise ValueError("O texto não passou pelo filtro de feedback de serviço.")

    professional = conn.execute(
        "SELECT id, feedback_kind, enabled FROM comment_professionals WHERE id=?",
        (professional_id,),
    ).fetchone()
    if not professional or not professional["enabled"]:
        raise ValueError("Profissional inexistente ou inativa.")
    if normalize_feedback_kind(professional["feedback_kind"]) != kind:
        raise ValueError("A profissional pertence à aba de feedback oposta.")

    member = conn.execute(
        """SELECT id, profile_status, COALESCE(comment_eligibility,'ELIGIBLE') AS comment_eligibility
           FROM members WHERE id=?""",
        (member_id,),
    ).fetchone()
    if not member or member["profile_status"] != "COMPLETE":
        raise ValueError("O membro precisa ter perfil COMPLETE.")
    if member["comment_eligibility"] == "DISSATISFIED":
        raise ValueError("Membro marcado como insatisfeito não pode receber atribuição.")
    if conn.execute("SELECT 1 FROM comment_member_usage WHERE member_id=?", (member_id,)).fetchone():
        raise ValueError("Este membro já foi usado em outro comentário.")
    if conn.execute("SELECT 1 FROM comment_bank WHERE author_member_id=?", (member_id,)).fetchone():
        raise ValueError("Este membro já está atribuído a outro comentário.")

    timestamp = now or utc_now()
    source_no = int(conn.execute("SELECT COALESCE(MAX(source_no),0)+1 FROM comment_bank").fetchone()[0])
    cursor = conn.execute(
        """INSERT INTO comment_bank(
               source_no, professional_id, age_band, comment_text, feedback_kind,
               author_member_id, status, scheduled_for, confirmed_at,
               confirmed_by_member_id, created_at, updated_at
           ) VALUES (?,?,?,?,?,?, 'AVAILABLE', NULL, ?, ?, ?, ?)""",
        (source_no, professional_id, "Manual", text, kind, member_id,
         timestamp, member_id, timestamp, timestamp),
    )
    return {"id": int(cursor.lastrowid), "source_no": source_no}


def assign_comment_professional(comment_id: int, professional_id: int) -> None:
    """Link an unassigned negative feedback to its later target ad."""
    with transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT id,professional_id,feedback_kind,status,comment_text FROM comment_bank WHERE id=?",
            (comment_id,),
        ).fetchone()
        if not row:
            raise ValueError("Comentário inexistente.")
        if normalize_feedback_kind(row["feedback_kind"]) != FEEDBACK_NEGATIVE:
            raise ValueError("Somente feedbacks negativos podem ser vinculados por esta ação.")
        if row["status"] != "AVAILABLE":
            raise ValueError("Este feedback já foi reservado ou enviado.")
        target = conn.execute(
            "SELECT id,feedback_kind FROM comment_professionals WHERE id=?",
            (professional_id,),
        ).fetchone()
        if not target or normalize_feedback_kind(target["feedback_kind"]) != FEEDBACK_NEGATIVE:
            raise ValueError("Anúncio de feedback negativo inválido.")
        duplicate = conn.execute(
            """SELECT 1 FROM comment_bank
               WHERE professional_id=? AND feedback_kind=? AND comment_text=? AND id<>?
               LIMIT 1""",
            (professional_id, FEEDBACK_NEGATIVE, row["comment_text"], comment_id),
        ).fetchone()
        if duplicate:
            raise ValueError("Este texto já está associado a esse anúncio negativo.")
        conn.execute(
            "UPDATE comment_bank SET professional_id=?, updated_at=? WHERE id=?",
            (professional_id, utc_now(), comment_id),
        )


def assign_comment_professionals(comment_ids: list[int], professional_id: int) -> dict[str, object]:
    """Link several available negative feedbacks in one transaction."""
    unique_ids = list(dict.fromkeys(int(comment_id) for comment_id in comment_ids))
    if not unique_ids:
        raise ValueError("Selecione pelo menos um feedback negativo.")
    linked = 0
    errors: list[dict[str, object]] = []
    with transaction(immediate=True) as conn:
        target = conn.execute(
            "SELECT id,feedback_kind FROM comment_professionals WHERE id=?",
            (professional_id,),
        ).fetchone()
        if not target or normalize_feedback_kind(target["feedback_kind"]) != FEEDBACK_NEGATIVE:
            raise ValueError("Anúncio de feedback negativo inválido.")
        for comment_id in unique_ids:
            row = conn.execute(
                "SELECT id,professional_id,feedback_kind,status,comment_text FROM comment_bank WHERE id=?",
                (comment_id,),
            ).fetchone()
            if not row:
                errors.append({"comment_id": comment_id, "error": "Comentário inexistente."})
                continue
            if normalize_feedback_kind(row["feedback_kind"]) != FEEDBACK_NEGATIVE:
                errors.append({"comment_id": comment_id, "error": "Somente feedbacks negativos podem ser vinculados."})
                continue
            if row["status"] != "AVAILABLE":
                errors.append({"comment_id": comment_id, "error": "Feedback já reservado ou enviado."})
                continue
            duplicate = conn.execute(
                """SELECT 1 FROM comment_bank
                   WHERE professional_id=? AND feedback_kind=? AND comment_text=? AND id<>?
                   LIMIT 1""",
                (professional_id, FEEDBACK_NEGATIVE, row["comment_text"], comment_id),
            ).fetchone()
            if duplicate:
                errors.append({"comment_id": comment_id, "error": "Texto já associado a esse anúncio."})
                continue
            conn.execute(
                "UPDATE comment_bank SET professional_id=?, updated_at=? WHERE id=?",
                (professional_id, utc_now(), comment_id),
            )
            linked += 1
    return {"linked": linked, "errors": errors}


def confirm_comment(conn, comment_id: int, member_id: int) -> bool:
    row=conn.execute("SELECT id,author_member_id,status FROM comment_bank WHERE id=?", (comment_id,)).fetchone()
    if not row: raise ValueError("Comentário inexistente.")
    if row["author_member_id"] != member_id: raise ValueError("Somente o autor associado pode confirmar este comentário.")
    if row["status"] != "AWAITING_CONFIRMATION": raise ValueError("Este comentário não está aguardando confirmação.")
    now=utc_now()
    conn.execute("UPDATE comment_bank SET status='AVAILABLE',scheduled_for=?,confirmed_at=?,confirmed_by_member_id=?,updated_at=? WHERE id=?", (now, now, member_id, now, comment_id))
    return True

def _due(limit=3):
    with get_connection() as conn:
        rows=conn.execute('''SELECT c.*,p.name professional_name,p.target_url,p.feedback_kind,m.first_name,m.last_name,m.username,m.birth_date,m.profile_status
                             FROM comment_bank c JOIN comment_professionals p ON p.id=c.professional_id
                             JOIN members m ON m.id=c.author_member_id
                             WHERE c.status='AVAILABLE'
                               AND c.scheduled_for IS NOT NULL
                               AND c.scheduled_for<=?
                               AND p.target_url IS NOT NULL
                               AND p.enabled=1
                               AND COALESCE(m.comment_eligibility,'ELIGIBLE')='ELIGIBLE'
                             ORDER BY c.scheduled_for LIMIT ?''',(utc_now(),limit)).fetchall()
        return [dict(r) for r in rows]

async def _publish(row: dict[str,Any]) -> int | None:
    adapter = MesLibertinesAdapter()
    async with async_playwright() as p:
        # A visible browser is required so Matheus can handle Cloudflare on the VPS.
        kwargs = {'headless': False}
        chromium = shutil.which('chromium') or shutil.which('chromium-browser')
        if chromium:
            kwargs['executable_path'] = chromium
        browser = await p.chromium.launch(**kwargs)
        context = await browser.new_context()
        if settings.twocaptcha_api_key:
            await install_turnstile_interceptor(context)
        page = await context.new_page()

        async def pause_for_manual_cloudflare() -> None:
            member_label = '{} {} (#{})'.format(
                row.get('first_name', ''), row.get('last_name', ''),
                row.get('author_member_id', '—')
            ).strip()
            await wait_for_manual_cloudflare(
                adapter=adapter,
                page=page,
                member_label=member_label,
                professional_name=row['professional_name'],
            )

        try:
            try:
                await adapter.login_member(page, row, settings.universal_password)
            except Exception as exc:
                if not is_cloudflare_intervention(exc):
                    raise
                await pause_for_manual_cloudflare()
                await adapter.login_member(page, row, settings.universal_password)

            await page.goto(row['target_url'], wait_until='domcontentloaded', timeout=45_000)
            await page.wait_for_timeout(1000)
            await adapter.accept_initial_age_gate(page, row['birth_date'], wait_ms=3000)
            try:
                await adapter._detect_protection(page)
            except Exception as exc:
                if not is_cloudflare_intervention(exc):
                    raise
                await pause_for_manual_cloudflare()
                await page.goto(row['target_url'], wait_until='domcontentloaded', timeout=45_000)
                await adapter._detect_protection(page)

            await adapter.publish_comment(
                page, row['comment_text'],
                manual_challenge_handler=pause_for_manual_cloudflare,
            )
        finally:
            await context.close()
            await browser.close()
    return record_comment_published(
        row['author_member_id'], row['professional_name'],
        row['target_url'], row['comment_text']
    )

async def run_due_comment_posts() -> int:
    with transaction(immediate=True) as conn:
        repaired = reconcile_scheduled_comments(conn)
    if repaired:
        log.info("Fila de comentários reconciliada: %s agenda(s) corrigida(s).", repaired)

    done=0
    for row in _due():
        # Claim inside the same write transaction that checks the cadence rules.
        # This prevents two due rows from the same professional, or two comments
        # closer than the global minimum interval, from passing the gate together.
        with transaction(immediate=True) as conn:
            now = _as_utc(utc_now())
            previous_comments = [
                dict(previous)
                for previous in conn.execute(
                    """
                    SELECT c.professional_id, p.name AS professional_name,
                           c.feedback_kind, c.submitted_at,
                           s.found_at AS scout_found_at,
                           s.checks_done AS scout_found_attempt,
                           s.status AS scout_status
                    FROM comment_bank c
                    JOIN comment_professionals p ON p.id=c.professional_id
                    LEFT JOIN comment_scouts s ON s.id=c.scout_id
                    WHERE c.submitted_at IS NOT NULL
                    """
                ).fetchall()
            ]
            if row.get('feedback_kind') == FEEDBACK_NEGATIVE:
                previous_negative = [
                    previous['submitted_at']
                    for previous in previous_comments
                    if previous.get('feedback_kind') == FEEDBACK_NEGATIVE
                    and previous.get('submitted_at')
                ]
                retry_at = negative_comment_retry_at(
                    now_utc=now,
                    previous_submissions=previous_negative,
                )
                block = ('negative_one_hour_interval', retry_at) if retry_at else None
            else:
                block = comment_publish_block(
                    professional_id=row['professional_id'],
                    professional_name=row.get('professional_name'),
                    now_utc=now,
                    previous_comments=previous_comments,
                )
            if block:
                _reason, retry_at = block
                conn.execute(
                    """UPDATE comment_bank
                       SET scheduled_for=?, last_error=NULL, updated_at=?
                       WHERE id=? AND status='AVAILABLE' AND scheduled_for=?""",
                    (retry_at, _iso(now), row['id'], row['scheduled_for']),
                )
                continue
            claimed = conn.execute(
                "UPDATE comment_bank SET scheduled_for=NULL, updated_at=? WHERE id=? AND status='AVAILABLE' AND scheduled_for=?",
                (_iso(now), row['id'], row['scheduled_for']),
            ).rowcount
        if claimed != 1:
            continue
        try:
            scout_id=await _publish(row)
            with transaction(immediate=True) as conn:
                now=utc_now()
                if row.get('feedback_kind') == FEEDBACK_NEGATIVE:
                    conn.execute("UPDATE comment_bank SET status='SUBMITTED_MODERATION',submitted_at=?,scout_id=?,last_error=NULL,updated_at=? WHERE id=?",(now,scout_id,now,row['id']))
                else:
                    conn.execute("UPDATE comment_bank SET status='AWAITING_CONFIRMATION',submitted_at=?,scout_id=?,last_error=NULL,updated_at=? WHERE id=?",(now,scout_id,now,row['id']))
                conn.execute("INSERT OR IGNORE INTO comment_member_usage(member_id,comment_id,used_at,outcome) VALUES (?,?,?,'SUBMITTED')",(row['author_member_id'],row['id'],now))
            if row.get('feedback_kind') == FEEDBACK_NEGATIVE:
                await send_discord(f"✅ **FEEDBACK NEGATIVO ENVIADO À MODERAÇÃO**\n{_member_notification_line(row)}\nPróximo envio negativo após 1 hora.")
            else:
                await send_discord(f"✅ **COMENTÁRIO ENVIADO**\n{_member_notification_line(row)}\nProfissional: **{row['professional_name']}**\nComentário #{row['source_no']} enviado.\nScout **#{scout_id}** iniciado automaticamente.")
        except asyncio.CancelledError: raise
        except Exception as exc:
            log.exception('Falha ao publicar comentário %s',row['id'])
            with transaction(immediate=True) as conn:
                conn.execute("UPDATE comment_bank SET status='AVAILABLE',scheduled_for=NULL,last_error=?,updated_at=? WHERE id=?",(f'{type(exc).__name__}: {exc}'[:1000],utc_now(),row['id']))
            try: await send_discord(f"🚨 **FALHA AO ENVIAR COMENTÁRIO**\n{_member_notification_line(row)}\nProfissional: **{row['professional_name']}**\nComentário #{row['source_no']} não foi consumido automaticamente. Revisão necessária.")
            except Exception: pass
        done+=1
    return done

async def comment_watch_loop() -> None:
    while True:
        try:
            await run_due_comment_posts()
        except asyncio.CancelledError: raise
        except Exception: log.exception('Erro no agendador de comentários.')
        await asyncio.sleep(settings.comment_scheduler_poll_seconds)

def group_comment_rows(professionals: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = {professional["id"]: [] for professional in professionals}
    for row in rows:
        grouped.setdefault(row["professional_id"], []).append(row)
    return [
        {**professional, "rows": grouped.get(professional["id"], [])}
        for professional in professionals
    ]


def comment_dashboard_data(
    feedback_kind: str = FEEDBACK_POSITIVE,
) -> dict[str,Any]:
    kind = normalize_feedback_kind(feedback_kind)
    if kind == FEEDBACK_POSITIVE:
        seed_comment_bank()
    with get_connection() as conn:
        pros=[]
        for p in conn.execute(
            'SELECT * FROM comment_professionals WHERE feedback_kind=? ORDER BY id',
            (kind,),
        ).fetchall():
            stats=dict(conn.execute(
                "SELECT COUNT(*) total,"
                "SUM(status='AVAILABLE') available,"
                "SUM(status='AVAILABLE') available_stage,"
                "SUM(status='AWAITING_CONFIRMATION') awaiting_confirmation,"
                "SUM(status='USED') used,"
                "SUM(author_member_id IS NOT NULL) mapped "
                "FROM comment_bank "
                "WHERE professional_id=? AND feedback_kind=? AND status<>'ARCHIVED_CONTENT'",
                (p['id'], kind),
            ).fetchone())
            nxt=conn.execute(
                "SELECT source_no,scheduled_for FROM comment_bank "
                "WHERE professional_id=? AND feedback_kind=? AND status='AVAILABLE' "
                "AND scheduled_for IS NOT NULL ORDER BY scheduled_for LIMIT 1",
                (p['id'], kind),
            ).fetchone()
            pros.append({
                **dict(p),
                **stats,
                'next': dict(nxt) if nxt else None,
                'is_unassigned': (
                    kind == FEEDBACK_NEGATIVE
                    and p['name'].casefold() == UNASSIGNED_NEGATIVE_PROFESSIONAL.casefold()
                ),
            })
        rows=[dict(r) for r in conn.execute(
            '''SELECT c.id,c.professional_id,c.source_no,c.age_band,c.comment_text,c.feedback_kind,
                      c.status,c.author_member_id,c.confirmation_requested_at,c.confirmed_at,
                      c.confirmed_by_member_id,c.scheduled_for,c.submitted_at,c.last_error,
                      p.name professional_name,m.username author_username
               FROM comment_bank c
               JOIN comment_professionals p ON p.id=c.professional_id
               LEFT JOIN members m ON m.id=c.author_member_id
               WHERE p.feedback_kind=? AND c.feedback_kind=? AND c.status<>'ARCHIVED_CONTENT'
               ORDER BY p.id,c.source_no''',
            (kind, kind),
        ).fetchall()]
        members=[dict(r) for r in conn.execute(
            """SELECT m.id,m.first_name,m.last_name,m.username
                FROM members m
                WHERE m.profile_status='COMPLETE'
                  AND COALESCE(m.comment_eligibility,'ELIGIBLE')='ELIGIBLE'
                  AND NOT EXISTS (SELECT 1 FROM comment_member_usage u WHERE u.member_id=m.id)
                  AND NOT EXISTS (SELECT 1 FROM comment_bank c WHERE c.author_member_id=m.id)
                ORDER BY m.id"""
        ).fetchall()]
    pros = group_comment_rows(pros, rows)
    return {
        'feedback_kind': kind,
        'professionals': pros,
        'rows': rows,
        'members': members,
        'timezone': settings.comment_timezone,
        'window': f'{settings.comment_window_start_hour:02d}:00–{settings.comment_window_end_hour:02d}:00',
        'require_author_confirmation': settings.comment_require_author_confirmation,
        'professional_interval_hours': settings.comment_professional_interval_minutes // 60,
        'minimum_interval_minutes': settings.comment_min_interval_minutes,
    }
