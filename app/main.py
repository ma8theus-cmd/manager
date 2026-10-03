from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from urllib.parse import parse_qs, quote_plus
from uuid import uuid4

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .config import settings
from .promising_members import promising_members_data
from .lead_batches import batch_dashboard_data, batch_content
from .database import init_db, transaction
from .discord_notifier import discord_watch_loop, queue_discord_notification
from .comment_automation import (
    comment_dashboard_data, comment_watch_loop, save_professional_url, add_professional_from_url,
    assign_comment_author, assign_comment_professional, assign_comment_professionals, confirm_comment, create_manual_comment, seed_comment_bank,
)
from .comment_automation.feedback import FEEDBACK_NEGATIVE, FEEDBACK_POSITIVE
from .comment_automation.live_challenge import (
    forward_click, forward_wheel, screenshot_for_session, session_is_valid,
)
from .scout import (
    record_comment_published,
    scout_dashboard_data,
    scout_member_options,
    scout_watch_loop,
)
from .services import (
    claim_profile_activation,
    comment_member_lists,
    dashboard_data,
    finish_worker_job,
    get_members_by_ids,
    get_pending_profile_activation_ids,
    get_remote_worker_jobs,
    mark_account_created,
    mark_account_created_automatic,
    mark_email_confirmed,
    mark_member_dissatisfied,
    mark_profile_complete,
    mark_profile_intervention,
    record_worker_job,
    reserve_batch,
    reset_to_pending,
    set_status,
    unmark_member_dissatisfied,
)
from .registration_destinations import get_destination
from .registration_network import vpn_command_prefix
from .worker_client import (
    WorkerClientError,
    get_job_status,
    submit_job,
    worker_is_configured,
)
from .profile_activation import (
    ProfileActivationAlreadyRunning,
    launch_profile_activation,
    profile_activation_is_running,
)

from .member_experience import (
    batch_dashboard_data as member_experience_dashboard_data,
    init_schema as init_member_experience_schema,
    launch_batch as launch_member_experience_batch,
    mark_batch_launch_failed,
    prepare_batch as prepare_member_experience_batch,
    start_batch as start_member_experience_batch,
)

app = FastAPI(
    title="MesLibertines Manager"
)

logger = logging.getLogger("meslibertines.central")

templates = Jinja2Templates(
    directory=str(
        settings.project_root
        / "app"
        / "templates"
    )
)


def _is_ajax(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


@app.on_event("startup")
async def startup():
    init_db()
    seed_comment_bank()
    with transaction(immediate=True) as conn:
        init_member_experience_schema(conn)

    app.state.comment_task = None
    app.state.scout_task = None
    app.state.worker_sync_task = None
    if settings.runs_comment_worker:
        app.state.comment_task = asyncio.create_task(comment_watch_loop())
        app.state.scout_task = asyncio.create_task(scout_watch_loop())

    # Discord monitoring remains available on both nodes.
    app.state.discord_task = asyncio.create_task(
        discord_watch_loop()
    )
    if worker_is_configured():
        app.state.worker_sync_task = asyncio.create_task(worker_sync_loop())


@app.on_event("shutdown")
async def shutdown():
    tasks = [
        getattr(app.state, "discord_task", None),
        getattr(app.state, "worker_sync_task", None),
        getattr(app.state, "comment_task", None),
        getattr(app.state, "scout_task", None),
    ]

    for task in tasks:
        if not task:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.get("/")
def dashboard(
    request: Request,
    msg: str | None = None,
    error: str | None = None,
):
    data = dashboard_data()
    member_comment_lists = comment_member_lists()
    data["comment_eligible_members"] = member_comment_lists["eligible"]
    data["dissatisfied_members"] = member_comment_lists["dissatisfied"]
    data["profile_activation_running"] = profile_activation_is_running()
    data["comments"] = comment_dashboard_data(FEEDBACK_POSITIVE)
    data["negative_comments"] = comment_dashboard_data(FEEDBACK_NEGATIVE)
    data["scout"] = scout_dashboard_data()
    data["scout_members"] = scout_member_options()
    data["promising"] = promising_members_data()
    data["lead_exports"] = batch_dashboard_data(settings.project_root / "research" / "comment_research.db")
    data["member_experience"] = member_experience_dashboard_data()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "data": data,
            "msg": msg,
            "error": error,
        },
    )


def _remote_job_id(member_id: int, action: str) -> str:
    return f"registration-{member_id}-{action}-{uuid4().hex[:12]}"


def _dispatch_remote_job(member: dict, action: str) -> str:
    route = str(member.get("registration_route") or "").upper()
    if route not in {"DIRECT", "VPN"}:
        raise RuntimeError("Membro remoto sem rota DIRECT/VPN definida.")
    job_id = _remote_job_id(int(member["id"]), action)
    state = submit_job(
        settings.worker_control_url,
        settings.worker_api_token,
        job_id,
        action,
        route,
        member,
    )
    if state.get("job_id") != job_id:
        raise WorkerClientError("Worker secundário não confirmou o identificador do trabalho.")
    record_worker_job(int(member["id"]), job_id, action)
    return job_id


def _apply_remote_job_state(member: dict, state: dict) -> str | None:
    worker_status = state.get("status")
    if worker_status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return None

    member_id = int(member["id"])
    action = member.get("registration_worker_action")
    result = state.get("result") or {}
    remote_status = result.get("remote_status")
    error = str(
        result.get("error")
        or state.get("error")
        or "Worker secundário encerrou o trabalho sem resultado."
    )[:1000]
    outcome = "manual_intervention"

    if worker_status == "SUCCEEDED" and action == "create_account":
        if remote_status == "WAITING_EMAIL_CONFIRMATION":
            if member.get("registration_status") != "WAITING_EMAIL_CONFIRMATION":
                mark_account_created_automatic(member_id)
            queue_discord_notification(
                f"account-created:{member_id}:{member['registration_worker_job_id']}",
                "✅ **CONTA CRIADA**\n"
                f"Membro: {member.get('first_name', '')} {member.get('last_name', '')} "
                f"(#{member_id})\n"
                f"Conta: {member.get('username', '')}\n"
                f"Rota: {member.get('registration_target', 'SECONDARY')}/"
                f"{member.get('registration_route', 'VPN')}\n"
                "Status: aguardando confirmação de email",
            )
            outcome = "account_created"
        else:
            set_status(member_id, "MANUAL_INTERVENTION", error)
    elif worker_status == "SUCCEEDED" and action == "activate_profile":
        if remote_status == "PROFILE_COMPLETE":
            mark_profile_complete(member_id)
            outcome = "profile_completed"
        else:
            mark_profile_intervention(member_id, error)
    elif action == "activate_profile":
        mark_profile_intervention(member_id, error)
    else:
        set_status(member_id, "MANUAL_INTERVENTION", error)

    finish_worker_job(member_id, str(member["registration_worker_job_id"]))
    return outcome


def sync_remote_jobs() -> dict[str, int]:
    stats = {
        "synchronized": 0,
        "accounts_created": 0,
        "profiles_completed": 0,
        "manual_intervention": 0,
    }
    if not worker_is_configured():
        return stats

    for member in get_remote_worker_jobs():
        job_id = str(member["registration_worker_job_id"])
        try:
            state = get_job_status(
                settings.worker_control_url,
                settings.worker_api_token,
                job_id,
            )
        except WorkerClientError:
            continue
        try:
            outcome = _apply_remote_job_state(member, state)
            if outcome is not None:
                stats["synchronized"] += 1
                if outcome == "account_created":
                    stats["accounts_created"] += 1
                elif outcome == "profile_completed":
                    stats["profiles_completed"] += 1
                else:
                    stats["manual_intervention"] += 1
        except Exception as exc:
            logger.warning("Falha ao sincronizar job remoto %s: %s", job_id, type(exc).__name__)
    return stats


async def worker_sync_loop():
    while True:
        try:
            await asyncio.to_thread(sync_remote_jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Erro no monitor de jobs remotos.")
        await asyncio.sleep(settings.worker_sync_poll_seconds)


def _prepare_batch(destination: str):
    try:
        definition = get_destination(destination)
        ids = reserve_batch(destination)

        if not ids:
            return RedirectResponse(
                "/?msg=Nao+ha+contas+pendentes+para+preparar",
                status_code=303,
            )

        if definition["target"] == "PRIMARY":
            script = settings.project_root / "scripts" / "prepare_batch.py"
            env = os.environ.copy()
            prefix = vpn_command_prefix()
            cmd = [
                *prefix,
                sys.executable,
                str(script),
                "--ids",
                *[str(member_id) for member_id in ids],
            ]
            subprocess.Popen(
                cmd,
                cwd=str(settings.project_root),
                env=env,
                start_new_session=True,
            )
            message = (
                f"Lote 1 reservado na VPS principal francesa com VPN: "
                f"{len(ids)} conta(s)."
            )
        else:
            sent = 0
            for member in get_members_by_ids(ids):
                try:
                    _dispatch_remote_job(member, "create_account")
                    sent += 1
                except Exception as exc:
                    set_status(
                        int(member["id"]),
                        "MANUAL_INTERVENTION",
                        f"Falha ao enviar para o worker secundário: {type(exc).__name__}",
                    )
            message = (
                f"{destination}: {sent}/{len(ids)} trabalho(s) enviado(s) "
                "à VPS secundária francesa."
            )

        return RedirectResponse(
            "/?msg=" + quote_plus(message),
            status_code=303,
        )

    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}",
            status_code=303,
        )


@app.post("/prepare-batch/{route}")
def prepare_batch(route: str):
    return _prepare_batch(route)


# Compatibilidade com links antigos: o endpoint sem rota aciona o lote direto.
@app.post("/prepare-batch")
def prepare_batch_legacy():
    return _prepare_batch("DIRECT")


@app.post("/worker/sync")
def sync_worker_jobs():
    try:
        stats = sync_remote_jobs()
        message = (
            f"{stats['synchronized']} trabalho(s) sincronizado(s): "
            f"{stats['accounts_created']} conta(s) criada(s), "
            f"{stats['profiles_completed']} perfil(is) ativado(s), "
            f"{stats['manual_intervention']} em intervenção manual."
        )
        return RedirectResponse(
            "/?msg=" + quote_plus(message),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            "/?error=" + quote_plus(str(exc)),
            status_code=303,
        )


@app.post("/member-experience/queue")
async def prepare_member_experience_queue(request: Request):
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        professional_id = int((form.get("professional_id", [""])[0] or "").strip())
        member_ids = [
            value.strip()
            for value in form.get("member_id", [])
            if value.strip()
        ]
        with transaction(immediate=True) as conn:
            batch_id = prepare_member_experience_batch(
                conn,
                professional_id,
                [int(value) for value in member_ids],
            )
        message = quote_plus(
            f"Fila preparada no lote #{batch_id}; nada foi iniciado automaticamente."
        )
        return RedirectResponse(
            f"/?msg={message}#member-experiences",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}#member-experiences",
            status_code=303,
        )


@app.post("/member-experience/start")
async def start_member_experience_queue(request: Request):
    batch_id = 0
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        batch_id = int((form.get("batch_id", [""])[0] or "").strip())
        with transaction(immediate=True) as conn:
            start_member_experience_batch(conn, batch_id)
        try:
            pid = launch_member_experience_batch(batch_id)
        except Exception as exc:
            mark_batch_launch_failed(batch_id, f"{type(exc).__name__}: {exc}")
            raise
        message = quote_plus(
            f"Fila #{batch_id} iniciada explicitamente (executor PID {pid})."
        )
        return RedirectResponse(
            f"/?msg={message}#member-experiences",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}#member-experiences",
            status_code=303,
        )


@app.post(
    "/members/{member_id}/account-created"
)
def account_created(member_id: int):
    try:
        mark_account_created(member_id)

        return RedirectResponse(
            "/?msg=Conta+marcada+como+criada",
            status_code=303,
        )

    except Exception as exc:
        return RedirectResponse(
            f"/?error={str(exc)}",
            status_code=303,
        )


@app.post(
    "/members/{member_id}/email-confirmed"
)
def email_confirmed(member_id: int, request: Request):
    ajax = _is_ajax(request)
    try:
        mark_email_confirmed(member_id)
        member = get_members_by_ids([member_id])[0]
        worker_job_id = None
        pid = launch_profile_activation([member_id])
        message = "Email confirmado. Ativação automática iniciada na VPS principal francesa."

        if ajax:
            return {
                "ok": True,
                "member_id": member_id,
                "registration_status": "PROFILE_PENDING" if worker_job_id else "EMAIL_CONFIRMED",
                "email_status": "CONFIRMED",
                "profile_status": "ACTIVATING",
                "profile_activation_pid": pid,
                "worker_job_id": worker_job_id,
                "message": message,
            }

        return RedirectResponse(
            "/?msg=" + quote_plus(message),
            status_code=303,
        )

    except Exception as exc:
        if ajax:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}",
            status_code=303,
        )


@app.post(
    "/members/{member_id}/reset"
)
def reset_member(member_id: int):
    try:
        reset_to_pending(member_id)

        return RedirectResponse(
            "/?msg=Membro+voltou+para+PENDING",
            status_code=303,
        )

    except Exception as exc:
        return RedirectResponse(
            f"/?error={str(exc)}",
            status_code=303,
        )


@app.get("/comments/cloudflare-live")
def cloudflare_live_page(request: Request):
    return templates.TemplateResponse(
        "cloudflare_live.html",
        {"request": request},
    )


@app.post("/comments/cloudflare-live/frame")
async def cloudflare_live_frame(request: Request):
    payload = await request.json()
    code = str(payload.get("code") or "")
    frame = await screenshot_for_session(code)
    if frame is None:
        if session_is_valid(code):
            raise HTTPException(status_code=503, detail="A página está recarregando; tente novamente.")
        raise HTTPException(status_code=404, detail="Sessão ao vivo expirada ou inválida.")
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/comments/cloudflare-live/input")
async def cloudflare_live_input(request: Request):
    payload = await request.json()
    code = str(payload.get("code") or "")
    action = payload.get("action")
    try:
        x, y = float(payload.get("x")), float(payload.get("y"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Coordenadas inválidas.")
    if action == "click":
        accepted = await forward_click(code, x, y)
    elif action == "wheel":
        try:
            delta_y = float(payload.get("delta_y"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Rolagem inválida.")
        accepted = await forward_wheel(code, x, y, delta_y)
    else:
        raise HTTPException(status_code=400, detail="Ação inválida.")
    if not accepted:
        raise HTTPException(status_code=403, detail="Sessão inválida ou ponto fora da tela.")
    return {"ok": True}


@app.post("/comments/professionals/add")
async def comments_add_professional(request: Request):
    try:
        raw=(await request.body()).decode('utf-8',errors='replace'); form=parse_qs(raw,keep_blank_values=True)
        name=(form.get('professional_name',[''])[0] or '').strip()
        url=(form.get('target_url',[''])[0] or '').strip()
        professional=add_professional_from_url(url, FEEDBACK_POSITIVE, name=name)
        return RedirectResponse('/?msg='+quote_plus(f"Profissional {professional['name']} adicionada automaticamente")+'#professionals',status_code=303)
    except Exception as exc:
        return RedirectResponse(f'/?error={quote_plus(str(exc))}#professionals',status_code=303)

@app.post("/comments/manual")
async def comments_manual_create(request: Request):
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        kind = (form.get("feedback_kind", [FEEDBACK_POSITIVE])[0] or FEEDBACK_POSITIVE).strip()
        professional_id = int((form.get("professional_id", [""])[0] or "").strip())
        member_id = int((form.get("member_id", [""])[0] or "").strip())
        comment_text = (form.get("comment_text", [""])[0] or "").strip()
        with transaction(immediate=True) as conn:
            created = create_manual_comment(
                conn,
                professional_id=professional_id,
                member_id=member_id,
                comment_text=comment_text,
                feedback_kind=kind,
            )
        anchor = "negative-comments" if kind == FEEDBACK_NEGATIVE else "comments"
        message = quote_plus(
            f"Comentário manual #{created['source_no']} atribuído ao membro e à profissional; envio não iniciado."
        )
        return RedirectResponse(f"/?msg={message}#{anchor}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/?error={quote_plus(str(exc))}#comments", status_code=303)

@app.post("/comments/professional-url")
async def comments_professional_url(request: Request):
    try:
        raw=(await request.body()).decode('utf-8',errors='replace'); form=parse_qs(raw,keep_blank_values=True)
        name=(form.get('name',[''])[0] or '').strip(); url=(form.get('target_url',[''])[0] or '').strip()
        save_professional_url(name,url,FEEDBACK_POSITIVE)
        return RedirectResponse('/?msg=URL+do+anuncio+salva#comments',status_code=303)
    except Exception as exc:
        return RedirectResponse(f'/?error={quote_plus(str(exc))}#comments',status_code=303)

@app.post("/negative-comments/professionals/add")
async def negative_comments_add_professional(request: Request):
    try:
        raw=(await request.body()).decode('utf-8',errors='replace'); form=parse_qs(raw,keep_blank_values=True)
        name=(form.get('professional_name',[''])[0] or '').strip()
        url=(form.get('target_url',[''])[0] or '').strip()
        professional=add_professional_from_url(url, FEEDBACK_NEGATIVE, name=name)
        return RedirectResponse('/?msg='+quote_plus(f"Profissional {professional['name']} adicionada à fila de feedbacks negativos")+'#negative-professionals',status_code=303)
    except Exception as exc:
        return RedirectResponse(f'/?error={quote_plus(str(exc))}#negative-professionals',status_code=303)

@app.post("/negative-comments/professional-url")
async def negative_comments_professional_url(request: Request):
    try:
        raw=(await request.body()).decode('utf-8',errors='replace'); form=parse_qs(raw,keep_blank_values=True)
        name=(form.get('name',[''])[0] or '').strip(); url=(form.get('target_url',[''])[0] or '').strip()
        save_professional_url(name,url,FEEDBACK_NEGATIVE)
        return RedirectResponse('/?msg=URL+do+anuncio+negativo+salva#negative-comments',status_code=303)
    except Exception as exc:
        return RedirectResponse(f'/?error={quote_plus(str(exc))}#negative-comments',status_code=303)

@app.post("/comments/{comment_id}/author")
async def comments_author(comment_id: int, request: Request):
    ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    try:
        raw=(await request.body()).decode('utf-8',errors='replace'); form=parse_qs(raw,keep_blank_values=True)
        value=(form.get('member_id',[''])[0] or '').strip()
        member_id = int(value) if value else None
        assign_comment_author(comment_id, member_id)
        if ajax:
            return {
                "ok": True,
                "comment_id": comment_id,
                "member_id": member_id,
                "requires_confirmation": settings.comment_require_author_confirmation,
            }
        return RedirectResponse('/?msg=Autor+do+feedback+associado#comments',status_code=303)
    except Exception as exc:
        if ajax:
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return RedirectResponse(f'/?error={quote_plus(str(exc))}#comments',status_code=303)

@app.post("/negative-comments/{comment_id}/professional")
async def negative_comments_assign_professional(comment_id: int, request: Request):
    try:
        raw=(await request.body()).decode('utf-8', errors='replace')
        form=parse_qs(raw, keep_blank_values=True)
        try:
            professional_id=int((form.get('professional_id', [''])[0] or '').strip())
        except (TypeError, ValueError):
            raise ValueError("Selecione um anúncio negativo válido.")
        assign_comment_professional(comment_id, professional_id)
        return RedirectResponse('/?msg=Feedback+negativo+vinculado+ao+anúncio#negative-comments', status_code=303)
    except Exception as exc:
        return RedirectResponse(f'/?error={quote_plus(str(exc))}#negative-comments', status_code=303)


@app.post("/negative-comments/bulk-professional")
async def negative_comments_assign_professional_bulk(request: Request):
    try:
        raw = (await request.body()).decode('utf-8', errors='replace')
        form = parse_qs(raw, keep_blank_values=True)
        try:
            professional_id = int((form.get('professional_id', [''])[0] or '').strip())
            comment_ids = [
                int(value.strip())
                for value in form.get('comment_id', [])
                if value.strip()
            ]
        except (TypeError, ValueError):
            raise ValueError("Selecione uma profissional e os feedbacks.")
        result = assign_comment_professionals(comment_ids, professional_id)
        linked = int(result["linked"])
        failed = len(result["errors"])
        message = f"{linked} feedback(s) negativo(s) vinculado(s) em lote."
        if failed:
            message += f" {failed} não puderam ser vinculados e permaneceram na fila."
        return RedirectResponse(
            f'/?msg={quote_plus(message)}#negative-comments',
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f'/?error={quote_plus(str(exc))}#negative-comments',
            status_code=303,
        )


@app.post("/comments/{comment_id}/confirm")
async def comments_confirm(comment_id: int, request: Request):
    try:
        raw=(await request.body()).decode("utf-8", errors="replace")
        form=parse_qs(raw, keep_blank_values=True)
        if (form.get("confirm", [""])[0] or "") != "yes":
            raise ValueError("A confirmação explícita é obrigatória.")
        try:
            member_id=int((form.get("member_id", [""])[0] or "").strip())
        except (TypeError, ValueError):
            raise ValueError("Autor inválido.")
        with transaction(immediate=True) as conn:
            confirm_comment(conn, comment_id, member_id)
        return RedirectResponse("/?msg=Autorização+registrada.+Comentário+enviado+para+a+fila#comments", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/?error={quote_plus(str(exc))}#comments", status_code=303)


@app.post("/scouts/register-published-comment")
async def register_published_comment_from_panel(request: Request):
    """Register an already-published comment and start its Scout."""
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)

        def field(name: str) -> str:
            return (form.get(name, [""])[0] or "").strip()

        try:
            member_id = int(field("member_id"))
        except (TypeError, ValueError):
            raise ValueError("Selecione um membro válido.")

        advertisement_title = field("advertisement_title")
        target_url = field("target_url")
        comment_text = field("comment_text")

        if not advertisement_title:
            raise ValueError("Informe o título do anúncio.")
        if not target_url:
            raise ValueError("Informe a URL do anúncio.")
        if not comment_text:
            raise ValueError("Informe o texto exato do comentário publicado.")

        scout_id = record_comment_published(
            member_id,
            advertisement_title,
            target_url,
            comment_text,
        )

        message = quote_plus(
            f"Comentário registrado. Scout #{scout_id} está ativo; "
            "a primeira verificação ocorrerá em aproximadamente 1 hora."
        )
        return RedirectResponse(f"/?msg={message}#scout", status_code=303)

    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}#scout",
            status_code=303,
        )


@app.post("/members/dissatisfied/mark")
async def mark_dissatisfied_member(request: Request):
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        member_id = int((form.get("member_id", [""])[0] or "").strip())
        reason = (form.get("reason", [""])[0] or "").strip()
        mark_member_dissatisfied(member_id, reason)
        return RedirectResponse(
            f"/?msg={quote_plus(f'Membro #{member_id} separado como insatisfeito.')}#dissatisfied-members",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}#dissatisfied-members",
            status_code=303,
        )


@app.post("/members/{member_id}/dissatisfied/unmark")
def unmark_dissatisfied_member(member_id: int):
    try:
        unmark_member_dissatisfied(member_id)
        return RedirectResponse(
            f"/?msg={quote_plus(f'Membro #{member_id} voltou à fila elegível.')}#dissatisfied-members",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}#dissatisfied-members",
            status_code=303,
        )


@app.post("/members/activate-pending-profiles")
def activate_pending_profiles():
    try:
        member_ids = get_pending_profile_activation_ids()
        if not member_ids:
            return RedirectResponse(
                "/?msg=Nao+ha+perfis+pendentes+para+ativar",
                status_code=303,
            )

        local_ids = [int(member["id"]) for member in get_members_by_ids(member_ids)]
        launch_profile_activation(local_ids)
        message = quote_plus(
            f"Ativações iniciadas na VPS principal francesa: {len(local_ids)} perfil(is)."
        )
        return RedirectResponse(f"/?msg={message}", status_code=303)
    except ProfileActivationAlreadyRunning as exc:
        return RedirectResponse(
            f"/?msg={quote_plus(str(exc))}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}",
            status_code=303,
        )


@app.post("/members/{member_id}/activate-profile")
def activate_profile(member_id: int, request: Request):
    ajax = _is_ajax(request)
    try:
        rows = get_members_by_ids([member_id])
        if not rows:
            raise ValueError("Membro inexistente.")
        member = rows[0]
        if member.get("email_status") != "CONFIRMED":
            raise ValueError("O email precisa estar confirmado antes de ativar o perfil.")
        if member.get("profile_status") == "COMPLETE":
            raise ValueError("O perfil já está completo.")

        worker_job_id = None
        pid = launch_profile_activation([member_id])
        message = "Ativação do perfil iniciada na VPS principal francesa."

        if ajax:
            return {
                "ok": True,
                "member_id": member_id,
                "profile_status": "ACTIVATING",
                "profile_activation_pid": pid,
                "worker_job_id": worker_job_id,
                "message": message,
            }

        return RedirectResponse(
            "/?msg=" + quote_plus(message),
            status_code=303,
        )
    except Exception as exc:
        if ajax:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return RedirectResponse(
            f"/?error={quote_plus(str(exc))}",
            status_code=303,
        )


@app.get("/members/{member_id}/status")
def member_status(member_id: int):
    rows = get_members_by_ids([member_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Membro inexistente.")
    member = rows[0]
    return {
        "ok": True,
        "member_id": member_id,
        "registration_status": member.get("registration_status"),
        "email_status": member.get("email_status"),
        "profile_status": member.get("profile_status"),
        "registration_target": member.get("registration_target"),
        "registration_route": member.get("registration_route"),
        "worker_job_id": member.get("registration_worker_job_id"),
        "worker_action": member.get("registration_worker_action"),
        "last_error": member.get("last_error") or "",
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "node_role": settings.node_role,
        "control_plane": settings.runs_control_plane,
        "comment_worker": settings.runs_comment_worker,
        "registration_lanes": {
            "primary_vpn": "76.13.55.107",
            "secondary_direct": "85.31.238.19",
            "secondary_vpn": "85.31.238.19",
        },
    }


@app.get("/leads/batches/{batch_id}/download")
def download_lead_batch(batch_id: int):
    content = batch_content(settings.project_root / "research" / "comment_research.db", batch_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Lote não encontrado.")
    return Response(content=content.encode("utf-8-sig"), media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="leads_lote_{batch_id:03d}.txt"'})
