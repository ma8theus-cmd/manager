import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from scripts import run_member_experience


class FakePage:
    def __init__(self, events):
        self.events = events

    async def goto(self, *args, **kwargs):
        self.events.append("goto")

    async def wait_for_timeout(self, *args, **kwargs):
        return None


class FakeAdapter:
    def __init__(self, events):
        self.events = events

    async def login_member(self, page, item, password):
        self.events.append("login")

    async def accept_initial_age_gate(self, page, birth_date, wait_ms):
        return None

    async def _detect_protection(self, page):
        return None

    async def click_private_ajouter(self, page):
        self.events.append("click")
        return False

    async def logout_member(self, page, item):
        self.events.append("logout")


class MemberExperienceExecutorTests(unittest.TestCase):
    def test_logout_runs_after_ajouter_even_without_visual_confirmation(self):
        events = []
        item = {
            "id": 47,
            "position": 1,
            "member_id": 1,
            "username": "member_one",
            "professional_name": "Amanda",
            "target_url": "https://example.test/amanda",
            "birth_date": "1980-01-01",
        }

        async def run():
            adapter = FakeAdapter(events)
            page = FakePage(events)
            with patch.object(run_member_experience, "record_login",
                              side_effect=lambda queue_id: events.append("record_login")),                  patch.object(run_member_experience, "record_click",
                              side_effect=lambda queue_id: events.append("record_click") or False),                  patch.object(run_member_experience, "record_logout",
                              side_effect=lambda queue_id: events.append("record_logout") or True),                  patch.object(run_member_experience, "record_failure") as failure,                  patch.object(run_member_experience, "_notify", new_callable=AsyncMock):
                result = await run_member_experience._process_item(adapter, page, item)
                failure.assert_not_called()
                return result

        result = asyncio.run(run())
        self.assertTrue(result)
        self.assertEqual(
            events,
            ["login", "record_login", "goto", "click",
             "record_click", "logout", "record_logout"],
        )

    def test_slow_discord_notification_does_not_block_the_executor(self):
        async def run():
            async def slow_send(message):
                await asyncio.sleep(1)
            with patch.object(run_member_experience, "send_discord", side_effect=slow_send):
                with patch.object(run_member_experience, "AJT_DISCORD_TIMEOUT_SECONDS", 0.01):
                    await run_member_experience._notify("teste")

        asyncio.run(run())

    def test_next_item_starts_immediately_after_logout(self):
        events = []
        item = {
            "id": 48,
            "position": 1,
            "member_id": 2,
            "username": "member_two",
            "professional_name": "Amanda",
            "target_url": "https://example.test/amanda",
            "birth_date": "1981-01-01",
        }

        async def run():
            adapter = FakeAdapter(events)
            page = FakePage(events)
            with patch.object(run_member_experience, "record_login",
                              side_effect=lambda queue_id: events.append("record_login")),                  patch.object(run_member_experience, "record_click",
                              side_effect=lambda queue_id: events.append("record_click") or False),                  patch.object(run_member_experience, "record_logout",
                              side_effect=lambda queue_id: events.append("record_logout") or False),                  patch.object(run_member_experience, "record_failure") as failure,                  patch.object(run_member_experience, "_notify", new_callable=AsyncMock),                  patch.object(run_member_experience.asyncio, "sleep",
                              new_callable=AsyncMock) as sleep:
                result = await run_member_experience._process_item(adapter, page, item)
                failure.assert_not_called()
                sleep.assert_not_awaited()
                return result

        result = asyncio.run(run())
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
