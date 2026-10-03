from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import time

from playwright.async_api import async_playwright

from app.config import settings
from app.database import transaction
from app.discord_notifier import send_discord
from app.member_experience import (
    claim_next_item,
    record_click,
    record_failure,
    record_login,
    record_logout,
    recover_interrupted_items,
    mark_batch_launch_failed,
)
from app.site_adapter.meslibertines import MesLibertinesAdapter


log = logging.getLogger("member_experience")
AJT_DISCORD_TIMEOUT_SECONDS = 5


async def _notify(message: str) -> None:
    try:
        await asyncio.wait_for(
            send_discord(message),
            timeout=AJT_DISCORD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("Notificação Discord excedeu o timeout e não bloqueou o fluxo Top 50.")
    except Exception:
        log.exception("Falha ao notificar o Discord.")


async def _process_item(adapter, page, item) -> bool:
    logged_in = False
    click_recorded = False
    completed = False
    stage = "login"
    failure: tuple[str, Exception] | None = None

    try:
        await adapter.login_member(
            page,
            item,
            settings.universal_password,
        )
        logged_in = True
        record_login(item["id"])
        await _notify(
            "🔐 **EXPERIÊNCIA PRIVADA — LOGIN CONCLUÍDO**\n"
            f"Profissional: **{item['professional_name']}**\n"
            f"Membro: **#{item['member_id']} · {item['username']}**\n"
            f"Posição: **{item['position']}**"
        )

        stage = "abrir anúncio"
        await page.goto(
            item["target_url"],
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        await page.wait_for_timeout(1_000)

        stage = "popup de idade"
        await adapter.accept_initial_age_gate(
            page,
            item["birth_date"],
            wait_ms=3_000,
        )

        stage = "proteção da página"
        await adapter._detect_protection(page)

        stage = "clique em Ajouter"
        visual_confirmation = await adapter.click_private_ajouter(page)

        stage = "registro do clique"
        all_clicked = record_click(item["id"])
        click_recorded = True
        confirmation_note = (
            "Confirmação visual recebida."
            if visual_confirmation
            else "Clique enviado; o site não alterou o indicador visual."
        )
        await _notify(
            "✅ **AJOUTER ACIONADO**\n"
            f"Profissional: **{item['professional_name']}**\n"
            f"Membro: **#{item['member_id']} · {item['username']}**\n"
            f"{confirmation_note}"
        )
        if all_clicked:
            await _notify(
                "🏁 **TODOS OS MEMBROS CLICARAM NO AJOUTER**\n"
                f"Profissional: **{item['professional_name']}**\n"
                "Todos os membros selecionados concluíram o clique; "
                "o executor ainda finalizará os logouts."
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failure = (stage, exc)
    finally:
        if logged_in:
            try:
                stage = "logout"
                await adapter.logout_member(page, item)
                if click_recorded:
                    completed = record_logout(item["id"])
                    if completed:
                        await _notify(
                            "🏁 **FILA CONCLUÍDA**\n"
                            f"Profissional: **{item['professional_name']}**\n"
                            "Todos os membros associados concluíram o Ajouter."
                        )
            except Exception as exc:
                if failure is None:
                    failure = (stage, exc)
                else:
                    previous_stage, previous_error = failure
                    failure = (
                        previous_stage,
                        RuntimeError(
                            f"{previous_error}; falha no logout: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )

    if failure is not None:
        failed_stage, exc = failure
        message = f"{type(exc).__name__}: {exc}"
        log.error(
            "Falha no item %s do lote %s na etapa %s: %s",
            item["id"],
            item.get("batch_id"),
            failed_stage,
            message,
        )
        record_failure(item["id"], message)
        await _notify(
            "🚨 **EXPERIÊNCIA PRIVADA — FALHA**\n"
            f"Profissional: **{item['professional_name']}**\n"
            f"Membro: **#{item['member_id']} · {item['username']}**\n"
            f"Etapa: **{failed_stage}**\n"
            f"Erro: **{message}**"
        )
        return False

    return True


async def _run_batch(batch_id: int) -> None:
    launch_kwargs = {"headless": settings.comment_headless}
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if system_chromium:
        launch_kwargs["executable_path"] = system_chromium

    adapter = MesLibertinesAdapter()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        try:
            while True:
                item = claim_next_item(batch_id)
                if not item:
                    return

                page = await context.new_page()
                try:
                    if not await _process_item(adapter, page, item):
                        return
                finally:
                    await page.close()
        finally:
            await context.close()
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", type=int, required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    max_restarts = 3
    for attempt in range(max_restarts + 1):
        try:
            with transaction(immediate=True) as conn:
                recovered = recover_interrupted_items(conn, args.batch_id)
            if recovered:
                log.warning(
                    "Recuperação automática: %s item(ns) reencaminhado(s) no lote %s.",
                    recovered,
                    args.batch_id,
                )
            asyncio.run(_run_batch(args.batch_id))
            return
        except Exception:
            log.exception(
                "Executor interrompido no lote %s (tentativa %s/%s).",
                args.batch_id,
                attempt + 1,
                max_restarts + 1,
            )
            with transaction(immediate=True) as conn:
                recovered = recover_interrupted_items(conn, args.batch_id)
            if not recovered or attempt >= max_restarts:
                mark_batch_launch_failed(
                    args.batch_id,
                    "Executor interrompido após esgotar as tentativas automáticas.",
                )
                raise
            time.sleep(2)


if __name__ == "__main__":
    main()
