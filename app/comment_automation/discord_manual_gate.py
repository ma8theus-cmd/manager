from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.discord_notifier import send_discord
from app.site_adapter.meslibertines import ManualIntervention
from app.comment_automation.manual_cloudflare import is_cloudflare_intervention
from app.comment_automation.live_challenge import clear_session, create_session
from app.comment_automation.twocaptcha import (
    TwoCaptchaApiError, restore_native_turnstile, solve_cloudflare_turnstile,
)


async def wait_for_manual_cloudflare(
    *, adapter, page, member_label: str, professional_name: str,
) -> None:
    mode = getattr(settings, "comment_captcha_mode", "auto")
    solver_error = None if mode == "manual" else "API_KEY_NOT_CONFIGURED"
    if mode != "manual" and settings.twocaptcha_api_key:
        solver_error = None
        try:
            solved = await solve_cloudflare_turnstile(
                page, settings.twocaptcha_api_key
            )
            if not solved:
                solver_error = "TURNSTILE_NOT_CAPTURED"
            if solved:
                solve_deadline = time.monotonic() + 60
                while time.monotonic() < solve_deadline:
                    try:
                        await adapter._detect_protection(page)
                        return
                    except ManualIntervention as exc:
                        if not is_cloudflare_intervention(exc):
                            raise
                        await asyncio.sleep(2)
                await restore_native_turnstile(page)
                solver_error = "TOKEN_NOT_CONFIRMED"
        except Exception as exc:
            await restore_native_turnstile(page)
            solver_error = (
                str(exc) if isinstance(exc, TwoCaptchaApiError)
                else type(exc).__name__
            )

    code = create_session(
        page, ttl_seconds=settings.comment_manual_wait_seconds
    )
    deadline = time.monotonic() + settings.comment_manual_wait_seconds
    try:
        message = (
            f"⏸️ **CLOUDFLARE — COMENTÁRIO PAUSADO**\n"
            f"Membro: **{member_label}**\nProfissional: **{professional_name}**\n"
            f"Código temporário do Manager: `{code}`\n"
            "Abra **Cloudflare ao vivo** no Manager, informe o código e marque "
            "a caixa na visualização."
        )
        if solver_error:
            message += (
                f"\n2Captcha não concluiu ({solver_error}); "
                "verificação manual disponível."
            )
        await send_discord(message)
        while True:
            try:
                await adapter._detect_protection(page)
                return
            except ManualIntervention as exc:
                if not is_cloudflare_intervention(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise ManualIntervention(
                        "Tempo de espera pela verificação manual do Cloudflare expirou."
                    ) from exc
                await asyncio.sleep(2)
    finally:
        clear_session(code)
