\
"""
Ferramenta de diagnóstico.

Ela abre a página de registro, imprime os atributos de inputs/selects e NÃO
preenche nem envia nada. Útil se o HTML do site mudar e algum seletor do adapter
precisar ser ajustado.
"""
from pathlib import Path
import sys
import asyncio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright
from app.config import settings


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(settings.registration_url, wait_until="domcontentloaded")

        print("\\nINPUTS")
        inputs = page.locator("input")
        for i in range(await inputs.count()):
            loc = inputs.nth(i)
            attrs = {}
            for name in ("type", "name", "id", "placeholder", "value", "aria-label"):
                attrs[name] = await loc.get_attribute(name)
            print(i, attrs)

        print("\\nSELECTS")
        selects = page.locator("select")
        for i in range(await selects.count()):
            loc = selects.nth(i)
            attrs = {}
            for name in ("name", "id", "title", "aria-label"):
                attrs[name] = await loc.get_attribute(name)
            print(i, attrs)

        print("\\nFeche o navegador quando terminar.")
        while browser.is_connected():
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run())
