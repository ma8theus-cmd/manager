from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright

from app.config import settings
from app.database import init_db
from app.discord_notifier import send_discord
from app.services import (
    claim_profile_activation,
    get_members_by_ids,
    mark_profile_complete,
    mark_profile_intervention,
)
from app.site_adapter.meslibertines import (
    ManualIntervention,
    MesLibertinesAdapter,
    RegistrationRateLimited,
)


PROFILE_ACTIVATION_MAX_ATTEMPTS = 3
PROFILE_ACTIVATION_RETRY_DELAY_SECONDS = 2

_NON_RETRYABLE_PROFILE_MARKERS = (
    "proteção/verificação",
    "captcha",
    "cloudflare",
    "too many requests",
    "rate limit",
    "access denied",
    "limite de cadastros",
    "credenciais rejeitadas",
    "senha incorreta",
    "incorrect password",
)


def _is_retryable_profile_error(exc: Exception) -> bool:
    if isinstance(exc, RegistrationRateLimited):
        return False

    if not isinstance(exc, ManualIntervention):
        return True

    message = str(exc).casefold()
    return not any(marker in message for marker in _NON_RETRYABLE_PROFILE_MARKERS)


async def run_with_controlled_retries(
    operation,
    *,
    max_attempts: int = PROFILE_ACTIVATION_MAX_ATTEMPTS,
    retry_delay: float = PROFILE_ACTIVATION_RETRY_DELAY_SECONDS,
):
    """Retry transient profile failures without retrying protection blocks."""
    if max_attempts < 1:
        raise ValueError("max_attempts precisa ser >= 1.")

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable_profile_error(exc):
                raise

            logging.warning(
                "Ativação de perfil falhou na tentativa %s/%s; "
                "repetindo com uma sessão nova: %s",
                attempt,
                max_attempts,
                exc,
            )
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)

    raise RuntimeError("Fluxo de retry terminou sem resultado.")


def setup_logging() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(settings.logs_dir / "profile_activation.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


async def notify_safely(message: str) -> None:
    try:
        await send_discord(message)
    except Exception:
        logging.exception("Falha ao enviar notificação de perfil ao Discord.")


async def run(member_ids: list[int]) -> None:
    setup_logging()
    init_db()

    if not settings.universal_password or settings.universal_password == "CHANGE_ME":
        raise RuntimeError("Configure UNIVERSAL_ACCOUNT_PASSWORD no .env antes de executar.")

    members = get_members_by_ids(member_ids)
    if not members:
        raise RuntimeError("Nenhum membro encontrado.")

    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
    adapter = MesLibertinesAdapter()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.headless)
        try:
            for member in members:
                member_id = member["id"]
                claimed = False
                context = None
                page = None
                async def activate_once():
                    nonlocal context, page

                    # Cada nova tentativa começa em um contexto limpo.
                    if context is not None:
                        await context.close()
                        context = None
                        page = None

                    context = await browser.new_context()
                    page = await context.new_page()

                    logging.info(
                        "Membro %s: iniciando ativação de perfil (homme, cidade=%s)",
                        member_id,
                        member["city_france"],
                    )

                    return await adapter.complete_member_profile(
                        page,
                        member,
                        settings.universal_password,
                    )

                try:
                    claim_profile_activation(member_id)
                    claimed = True
                    await run_with_controlled_retries(
                        activate_once,
                        max_attempts=PROFILE_ACTIVATION_MAX_ATTEMPTS,
                        retry_delay=PROFILE_ACTIVATION_RETRY_DELAY_SECONDS,
                    )

                    mark_profile_complete(member_id)
                    logging.info("Membro %s: perfil ativado, validado e logout confirmado.", member_id)
                    await notify_safely(
                        "✅ **PERFIL ATIVADO**\n"
                        f"Membro: {member['first_name']} {member['last_name']}\n"
                        f"Conta: {member['username']}\n"
                        "Je suis: homme\n"
                        f"Ville: {member['city_france']}\n"
                        "Status: PROFILE_COMPLETE\n"
                        "Sessão: desconectada"
                    )

                except ManualIntervention as exc:
                    if claimed:
                        mark_profile_intervention(member_id, str(exc))
                    if page is not None:
                        shot = settings.screenshots_dir / f"member_{member_id:04d}_profile_manual.png"
                        try:
                            await page.screenshot(path=str(shot), full_page=True)
                        except Exception:
                            pass
                    logging.warning("Membro %s: %s", member_id, exc)
                    await notify_safely(
                        "⚠️ **PERFIL REQUER INTERVENÇÃO**\n"
                        f"Membro: {member['first_name']} {member['last_name']}\n"
                        f"Conta: {member['username']}\n"
                        f"Motivo: {exc}"
                    )

                except ValueError as exc:
                    # Usually means already complete/activating or an invalid state.
                    logging.warning("Membro %s: %s", member_id, exc)

                except Exception as exc:
                    msg = f"{type(exc).__name__}: {exc}"
                    if claimed:
                        mark_profile_intervention(member_id, msg)
                    if page is not None:
                        shot = settings.screenshots_dir / f"member_{member_id:04d}_profile_error.png"
                        try:
                            await page.screenshot(path=str(shot), full_page=True)
                        except Exception:
                            pass
                    logging.exception("Membro %s: falha técnica na ativação do perfil", member_id)
                    await notify_safely(
                        "⚠️ **ERRO NA ATIVAÇÃO DO PERFIL**\n"
                        f"Membro: {member['first_name']} {member['last_name']}\n"
                        f"Conta: {member['username']}\n"
                        f"Motivo: {msg}"
                    )

                finally:
                    if context is not None:
                        await context.close()
        finally:
            if browser.is_connected():
                await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="+", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.ids))


if __name__ == "__main__":
    main()
