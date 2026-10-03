from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright
from app.config import settings
from app.site_adapter.meslibertines import MesLibertinesAdapter

MEMBER_ID = 1
TEST_CITY = "Clermont-Ferrand"


def get_member() -> dict:
    conn = sqlite3.connect(PROJECT_ROOT / "data/members.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM members WHERE id=?", (MEMBER_ID,)).fetchone()
    conn.close()
    if row is None:
        raise RuntimeError("Membro de teste não encontrado")
    return dict(row)


async def save_city(adapter, page, member, city: str) -> str:
    scope = await adapter.open_profile_editor(page, member)
    chosen = await adapter._select_profile_city(page, scope, city)
    save = await adapter._find_profile_save_button(page)
    if save is None:
        raise RuntimeError("Botão Enregistrer não encontrado")
    await save.click()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)
    return chosen


async def ensure_login(adapter, page, member) -> None:
    if not await adapter._is_logged_in(page, member["username"]):
        await adapter.login_member(page, member, settings.universal_password)


async def main() -> None:
    member = get_member()
    original = member["city_france"]
    adapter = MesLibertinesAdapter()
    target_saved = False
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.headless)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await adapter.login_member(page, member, settings.universal_password)
            chosen = await save_city(adapter, page, member, TEST_CITY)
            target_saved = True
            print(f"TEST_CITY_SAVED={chosen}")

            await ensure_login(adapter, page, member)
            restored = await save_city(adapter, page, member, original)
            print(f"ORIGINAL_CITY_RESTORED={restored}")

            await adapter.logout_member(page, member)
            print("LOGOUT_OK")
        finally:
            if target_saved:
                try:
                    await ensure_login(adapter, page, member)
                    scope = await adapter.open_profile_editor(page, member)
                    city_input = await adapter._find_profile_city_input(scope)
                    current = (await city_input.input_value()).strip() if city_input else ""
                    if adapter.normalize_city_for_search(current) != adapter.normalize_city_for_search(original):
                        restored = await save_city(adapter, page, member, original)
                        print(f"FINALLY_RESTORED={restored}")
                except Exception as exc:
                    print(f"RESTORE_WARNING={type(exc).__name__}: {exc}")
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
