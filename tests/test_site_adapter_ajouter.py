import asyncio
import unittest
from unittest.mock import AsyncMock

from app.site_adapter.meslibertines import MesLibertinesAdapter


class EmptyLocator:
    async def count(self):
        return 0


class AjouterLocator:
    def __init__(self):
        self.clicked = False

    async def count(self):
        return 1

    def nth(self, index):
        return self

    async def is_visible(self):
        return True

    def locator(self, selector):
        return EmptyLocator()

    async def get_attribute(self, name):
        return "ajouter-button" if name == "class" else None

    async def click(self, **kwargs):
        self.clicked = True


class FakePage:
    def __init__(self, button):
        self.button = button
        self.empty = EmptyLocator()
        self.calls = 0

    def get_by_text(self, *args, **kwargs):
        self.calls += 1
        return self.button if self.calls == 1 else self.empty

    async def wait_for_timeout(self, milliseconds):
        return None


class AjouterAdapterTests(unittest.TestCase):
    def test_click_returns_unconfirmed_without_raising_after_dispatch(self):
        button = AjouterLocator()
        page = FakePage(button)
        adapter = MesLibertinesAdapter()
        adapter._detect_protection = AsyncMock()

        result = asyncio.run(adapter.click_private_ajouter(page))

        self.assertFalse(result)
        self.assertTrue(button.clicked)


if __name__ == "__main__":
    unittest.main()

class LogoutLocator:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind

    @property
    def first(self):
        return self

    async def count(self):
        if self.kind == "profile":
            return 0 if self.page.logged_out else 1
        return 1 if self.page.profile_open and not self.page.logged_out else 0

    def nth(self, index):
        return self

    async def is_visible(self):
        return (await self.count()) == 1

    def locator(self, selector):
        return self

    async def click(self, **kwargs):
        if self.kind == "profile":
            self.page.profile_clicked = True
            self.page.profile_open = True
        else:
            self.page.logout_clicked = True
            self.page.logged_out = True

    async def evaluate(self, script):
        await self.click()


class FakeLogoutPage:
    def __init__(self):
        self.profile_open = False
        self.logged_out = False
        self.profile_clicked = False
        self.logout_clicked = False
        self.profile = LogoutLocator(self, "profile")
        self.logout = LogoutLocator(self, "logout")

    def _pattern(self, value):
        return getattr(value, "pattern", str(value))

    def locator(self, selector):
        if selector == "body":
            return self.locator_body()
        return EmptyLocator()

    def get_by_role(self, role, name=None):
        pattern = self._pattern(name)
        if "MON" in pattern.upper() and "PROFIL" in pattern.upper():
            return self.profile
        if "D[ée]connexion" in pattern or "Deconnexion" in pattern:
            return self.logout
        return EmptyLocator()

    def get_by_text(self, value, **kwargs):
        pattern = self._pattern(value)
        if "MON" in pattern.upper() and "PROFIL" in pattern.upper():
            return self.profile
        if "D[ée]connexion" in pattern or "Deconnexion" in pattern:
            return self.logout
        return EmptyLocator()

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, milliseconds):
        return None

    def body_text(self):
        return "SE CONNECTER INSCRIPTION" if self.logged_out else "MON PROFIL"

    def locator_body(self):
        page = self

        class Body:
            async def inner_text(self):
                return page.body_text()

        return Body()


class LogoutAdapterTests(unittest.TestCase):
    def test_logout_opens_mon_profil_before_deconnexion(self):
        page = FakeLogoutPage()
        adapter = MesLibertinesAdapter()

        asyncio.run(adapter.logout_member(page, {"id": 1}))

        self.assertTrue(page.profile_clicked)
        self.assertTrue(page.logout_clicked)
        self.assertTrue(page.logged_out)
