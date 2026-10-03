from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright
from app.site_adapter.meslibertines import MesLibertinesAdapter
from app.config import settings
from app.database import get_connection, init_db, utc_now
from app.services import claim_profile_activation, mark_profile_complete, mark_profile_intervention


PROFILE_HTML = r"""
<!doctype html>
<html><head><style>
#profile { width: 650px; padding: 20px; }
.row { margin: 12px 0; position: relative; }
#city { width: 180px; height: 28px; }
#suggestions { position:absolute; left:80px; top:28px; width:180px; margin:0; padding:0; list-style:none; border:1px solid #999; background:white; z-index:9; }
#suggestions li { padding:4px; }
</style></head>
<body>
<form id="profile">
  <div class="row" id="gender-row">
    <span>Je suis:</span>
    <div class="radio-option"><label for>homme</label><span class="mcf-fake-radio"><input style="display:none" type="radio" name="data[gender]" value="m"></span></div>
    <div class="radio-option"><label for>femme</label><span class="mcf-fake-radio active"><input style="display:none" type="radio" name="data[gender]" value="f" checked></span></div>
    <div class="radio-option"><label for>couple</label><span class="mcf-fake-radio"><input style="display:none" type="radio" name="data[gender]" value="c"></span></div>
    <div class="radio-option"><label for>trans</label><span class="mcf-fake-radio"><input style="display:none" type="radio" name="data[gender]" value="t"></span></div>
  </div>
  <div class="row" id="city-row">
    <label for="city">Ville:</label>
    <input id="city" name="ville" value="Paris" autocomplete="off">
    <ul id="suggestions" class="autocompleter-choices" style="display:none"></ul>
  </div>
  <button id="save" type="button" onclick="window.saved=true">Enregistrer</button>
</form>
<script>
// Reproduce the live MesLibertines custom radio behavior discovered in DevTools:
// clicking the hidden input itself is ineffective; clicking the parent fake-radio
// toggles the corresponding native input.
document.querySelectorAll('input[name="data[gender]"]').forEach(input => {
  input.addEventListener('click', e => e.preventDefault());
  input.parentElement.addEventListener('click', e => {
    if (e.target === input) return;
    document.querySelectorAll('input[name="data[gender]"]').forEach(other => other.checked = false);
    input.checked = true;
  });
});
const city = document.getElementById('city');
const ul = document.getElementById('suggestions');
city.addEventListener('input', () => {
  const q = city.value.toLowerCase().trim();
  ul.innerHTML = '';
  if (q.includes('clermont ferrand')) {
    ['Clermont-ferrand', 'Clermont-l’Hérault'].forEach(name => {
      const li = document.createElement('li');
      li.textContent = name;
      li.onclick = () => { city.value = name; ul.style.display = 'none'; window.citySelected = name; };
      ul.appendChild(li);
    });
    ul.style.display = 'block';
  }
});
</script>
</body></html>
"""


DELAYED_AGE = r"""
<html><body>
<div id="agepopup" style="display:none">
  <div id="authorization">J'ai lu les terms and conditions</div>
  <select id="d"><option value="9">9</option></select>
  <select id="m"><option value="2">February</option></select>
  <select id="y"><option value="1998">1998</option></select>
  <button id="ok" onclick="document.getElementById('agepopup').style.display='none'">OK</button>
</div>
<script>
setTimeout(() => document.getElementById('agepopup').style.display='block', 700);
setTimeout(() => {
  const label = document.createElement('label');
  label.innerHTML = '<input type="checkbox" id="agecheck" style="display:none"> Je certifie être d\'accord avec les règles et j\'ai lu les terms and conditions';
  document.getElementById('authorization').replaceWith(label);
}, 1300);
</script>
</body></html>
"""

LOGOUT_HTML = r"""
<!doctype html><html><head><style>
#logout { display:none; }
#menu:hover #logout { display:block; }
</style></head><body>
<div>BIENVENUE GERARD_GIRAUD27 DANS VOTRE ESPACE PRIVÉ!</div>
<div id="menu">MON PROFIL
  <a id="logout" href="#" onclick="document.body.innerHTML='<a>SE CONNECTER</a>'; return false;">DÉCONNEXION GERARD_GIRAUD27</a>
</div>
</body></html>
"""


async def main() -> None:
    adapter = MesLibertinesAdapter()
    assert adapter.normalize_city_for_search('Évry-sur-Seine') == 'evry sur seine'
    assert adapter.normalize_city_for_search('Clermont-Ferrand') == 'clermont ferrand'

    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if system_chromium:
            launch_kwargs["executable_path"] = system_chromium
        browser = await p.chromium.launch(**launch_kwargs)
        page = await browser.new_page(viewport={"width": 1100, "height": 800})

        await page.set_content(DELAYED_AGE)
        handled = await adapter.accept_initial_age_gate(page, '1998-02-09', wait_ms=2_000)
        assert handled is True
        assert await page.locator('#agecheck').is_checked()

        await page.set_content(LOGOUT_HTML)
        await adapter.logout_member(page, {"username": "Gerard_Giraud27", "birth_date": "1998-02-09"})
        assert 'SE CONNECTER' in await page.locator('body').inner_text()

        await page.set_content(PROFILE_HTML)
        scope = await adapter._profile_scope(page)
        assert scope is not None
        await adapter._select_profile_gender_homme(scope)
        assert await page.locator('input[value="m"]').is_checked()
        assert not await page.locator('input[value="f"]').is_checked()

        chosen = await adapter._select_profile_city(page, scope, 'Clermont-Ferrand')
        assert adapter.normalize_city_for_search(chosen) == 'clermont ferrand'
        assert await page.locator('#city').input_value() == 'Clermont-ferrand'
        assert await page.evaluate('window.citySelected') == 'Clermont-ferrand'

        save = await adapter._find_profile_save_button(page)
        assert save is not None
        await save.click()
        assert await page.evaluate('window.saved') is True

        await browser.close()

    original_db = settings.database_path
    try:
        with TemporaryDirectory(prefix='meslibertines_profile_test_') as tmp:
            object.__setattr__(settings, 'database_path', Path(tmp) / 'members_test.db')
            init_db()
            now = utc_now()
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO members(
                        id, first_name, last_name, birth_date, city_france,
                        username, email, registration_status, email_status,
                        profile_status, created_at, updated_at
                    ) VALUES (
                        1, 'Jean', 'Profil', '1990-01-01', 'Évry-sur-Seine',
                        'jean.profil', 'jean@example.invalid', 'EMAIL_CONFIRMED',
                        'CONFIRMED', 'PENDING', ?, ?
                    )
                    """,
                    (now, now),
                )
            claim_profile_activation(1)
            with get_connection() as conn:
                row = conn.execute('SELECT registration_status, profile_status FROM members WHERE id=1').fetchone()
                assert row['registration_status'] == 'PROFILE_PENDING'
                assert row['profile_status'] == 'ACTIVATING'
            mark_profile_complete(1)
            with get_connection() as conn:
                row = conn.execute('SELECT registration_status, profile_status FROM members WHERE id=1').fetchone()
                assert row['registration_status'] == 'PROFILE_COMPLETE'
                assert row['profile_status'] == 'COMPLETE'

            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO members(
                        id, first_name, last_name, birth_date, city_france,
                        username, email, registration_status, email_status,
                        profile_status, created_at, updated_at
                    ) VALUES (
                        2, 'Marc', 'Retry', '1991-01-01', 'Paris',
                        'marc.retry', 'marc@example.invalid', 'PROFILE_PENDING',
                        'CONFIRMED', 'ACTIVATING', ?, ?
                    )
                    """,
                    (now, now),
                )
            mark_profile_intervention(2, 'teste controlado')
            with get_connection() as conn:
                row = conn.execute('SELECT registration_status, profile_status, last_error FROM members WHERE id=2').fetchone()
                assert row['registration_status'] == 'EMAIL_CONFIRMED'
                assert row['profile_status'] == 'MANUAL_INTERVENTION'
                assert row['last_error'] == 'teste controlado'
    finally:
        object.__setattr__(settings, 'database_path', original_db)

    print('OK - perfil offline: popup tardio, radio Homme via parent mcf-fake-radio, cidade, Enregistrer, logout e estados do banco validados.')


if __name__ == '__main__':
    asyncio.run(main())
