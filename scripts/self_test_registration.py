from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright
from app.site_adapter.meslibertines import ManualIntervention, MesLibertinesAdapter, RegistrationRateLimited

MEMBER = {
    "id": 999999,
    "username": "test.selector",
    "email": "selector@example.invalid",
    "city_france": "Paris",
    "birth_date": "1998-02-09",
}

POPUP = """
<html><body>
<div id="agepopup">
  <div>Je certifie être d'accord avec les règles qui précèdent et je signe électroniquement mon accord :</div>
  <label><input type="checkbox" id="agecheck"> J'ai lu les terms and conditions</label>
  <div>Veuillez saisir votre date de naissance</div>
  <select id="d"><option>day</option><option value="9">9</option></select>
  <select id="m"><option>Month</option><option value="2">February</option></select>
  <select id="y"><option>Year</option><option value="1998">1998</option></select>
  <button>ANNULER</button>
  <button id="ok" onclick="document.getElementById('agepopup').style.display='none'">OK</button>
</div>
</body></html>
"""

REGISTRATION = """
<html><body>
<form id="reg" onsubmit="event.preventDefault(); this.remove(); document.body.insertAdjacentHTML('beforeend','<div id=success>Welcome</div>'); history.pushState({},'', '/welcome');">
  <label>Member <input type="radio" name="type" value="member"></label>
  <label>Choose a username: <input name="username"></label>
  <label>Your email: <input name="email"></label>
  <label>Choose password: <input type="password" name="p1"></label>
  <label>Repeat password: <input type="password" name="p2"></label>
  <label>Home country:<select name="country"><option value="fr">France</option></select></label>
  <label>Home city:<input name="city"></label>
  <select name="day"><option value="9">9</option></select>
  <select name="month"><option value="2">February</option></select>
  <select name="year"><option value="1998">1998</option></select>
  <label>By signing up I accept the Terms and Conditions: <input type="checkbox" name="terms"></label>
  <button type="submit">Register!</button>
</form>
</body></html>
"""

RATE_LIMITED = """
<html><body>
<form id="reg"><input type="password"><input type="password"><button type="submit">Register!</button></form>
<div class="error">There are too many registrations from your IP today</div>
</body></html>
"""

SLOW_SUCCESS = """
<html><body>
<form id="reg" onsubmit="event.preventDefault(); setTimeout(()=>this.remove(), 1500)">
<input type="password"><input type="password"><button type="submit">Register!</button>
</form></body></html>
"""

USERNAME_CONFLICT = """
<html><body><form><input type="password"><input type="password"><button>Register!</button></form>
<div>Username already exists</div></body></html>
"""

EMAIL_CONFLICT = """
<html><body><form><input type="password"><input type="password"><button>Register!</button></form>
<div>Email is already in use</div></body></html>
"""

AMBIGUOUS = """
<html><body>
<form id="reg"><input type="password"><input type="password"><button type="submit">Register!</button></form>
</body></html>
"""


async def load_register_page(page, html: str) -> None:
    await page.unroute("**/*")
    async def handler(route):
        await route.fulfill(status=200, content_type="text/html", body=html)
    await page.route("https://local.test/**", handler)
    await page.goto("https://local.test/users/register")


async def main() -> None:
    adapter = MesLibertinesAdapter()
    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if system_chromium:
            launch_kwargs["executable_path"] = system_chromium

        browser = await p.chromium.launch(**launch_kwargs)
        page = await browser.new_page()

        await page.set_content(POPUP)
        assert await adapter.accept_initial_age_gate(page, MEMBER["birth_date"])
        assert await page.locator("#agecheck").is_checked()
        assert await page.locator("#d").input_value() == "9"
        assert await page.locator("#m").input_value() == "2"
        assert await page.locator("#y").input_value() == "1998"

        await page.set_content(REGISTRATION)
        form = await adapter._registration_form(page)
        assert form is not None
        await adapter.fill_member_form(form, MEMBER, "test-password")
        await adapter.accept_terms(form)
        assert await page.locator('input[name="terms"]').is_checked()
        await adapter.submit_registration(page, form)
        await adapter._confirm_registration_advanced(page)
        assert await page.locator("#success").count() == 1

        await page.set_content(RATE_LIMITED)
        try:
            await adapter._detect_protection(page)
            raise AssertionError("rate limit deveria ter sido detectado")
        except RegistrationRateLimited:
            pass


        await load_register_page(page, SLOW_SUCCESS)
        form = await adapter._registration_form(page); assert form is not None
        await adapter.submit_registration(page, form)
        await adapter._confirm_registration_advanced(page)

        for html, expected in ((USERNAME_CONFLICT, "username"), (EMAIL_CONFLICT, "email"), (AMBIGUOUS, "Register foi enviado")):
            await load_register_page(page, html)
            try:
                await adapter._confirm_registration_advanced(page)
                raise AssertionError("deveria exigir intervenção")
            except ManualIntervention as exc:
                assert expected.lower() in str(exc).lower()

        await browser.close()

    print("OK - teste local concluído. Nenhuma conexão com MesLibertines foi feita.")


if __name__ == "__main__":
    asyncio.run(main())
