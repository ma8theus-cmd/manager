import unittest
from unittest.mock import AsyncMock, patch

from app.comment_automation.discord_manual_gate import wait_for_manual_cloudflare
from app.comment_automation.manual_cloudflare import is_cloudflare_intervention
from app.site_adapter.meslibertines import ManualIntervention


class FakeAdapter:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    async def _detect_protection(self, page):
        self.calls += 1
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error


class ManualCloudflarePauseTests(unittest.IsolatedAsyncioTestCase):
    def test_only_cloudflare_protection_is_pauseable(self):
        self.assertTrue(is_cloudflare_intervention(ManualIntervention("Proteção/verificação detectada: cloudflare")))
        self.assertFalse(is_cloudflare_intervention(ManualIntervention("Proteção/verificação detectada: captcha")))
        self.assertFalse(is_cloudflare_intervention(RuntimeError("cloudflare")))

    @patch("app.comment_automation.discord_manual_gate.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.send_discord", new_callable=AsyncMock)
    async def test_sends_webhook_alert_and_waits_until_challenge_clears(self, send, sleep):
        adapter = FakeAdapter([ManualIntervention("Proteção/verificação detectada: cloudflare"), None])
        await wait_for_manual_cloudflare(
            adapter=adapter, page=object(), member_label="Membro #42",
            professional_name="Brenda",
        )
        send.assert_awaited_once()
        self.assertIn("Membro #42", send.await_args.args[0])
        sleep.assert_awaited_once_with(2)
        self.assertEqual(adapter.calls, 2)


    @patch("app.comment_automation.discord_manual_gate.send_discord", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.solve_cloudflare_turnstile", new_callable=AsyncMock, return_value=True)
    async def test_uses_configured_solver_before_manual_gate(self, solve, send):
        from types import SimpleNamespace
        with patch("app.comment_automation.discord_manual_gate.settings",
                   SimpleNamespace(twocaptcha_api_key="test-key", comment_manual_wait_seconds=60)):
            adapter = FakeAdapter([None])
            await wait_for_manual_cloudflare(
                adapter=adapter, page=object(), member_label="Membro #42",
                professional_name="Brenda",
            )
        solve.assert_awaited_once()
        send.assert_not_awaited()

    @patch("app.comment_automation.discord_manual_gate.restore_native_turnstile", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.solve_cloudflare_turnstile", new_callable=AsyncMock, side_effect=RuntimeError("API_DOWN"))
    @patch("app.comment_automation.discord_manual_gate.send_discord", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.asyncio.sleep", new_callable=AsyncMock)
    async def test_solver_failure_restores_manual_fallback(self, sleep, send, solve, restore):
        from types import SimpleNamespace
        with patch("app.comment_automation.discord_manual_gate.settings",
                   SimpleNamespace(twocaptcha_api_key="test-key", comment_manual_wait_seconds=60)):
            adapter = FakeAdapter([
                ManualIntervention("Proteção/verificação detectada: cloudflare"), None
            ])
            await wait_for_manual_cloudflare(
                adapter=adapter, page=object(), member_label="Membro #42",
                professional_name="Brenda",
            )
        solve.assert_awaited_once()
        restore.assert_awaited_once()
        send.assert_awaited_once()
        self.assertIn("Membro #42", send.await_args.args[0])


    @patch("app.comment_automation.discord_manual_gate.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.send_discord", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.solve_cloudflare_turnstile", new_callable=AsyncMock, return_value=False)
    async def test_uncaptured_turnstile_reports_reason_in_manual_fallback(self, solve, send, sleep):
        from types import SimpleNamespace
        with patch("app.comment_automation.discord_manual_gate.settings",
                   SimpleNamespace(twocaptcha_api_key="test-key", comment_manual_wait_seconds=60)):
            adapter = FakeAdapter([
                ManualIntervention("Proteção/verificação detectada: cloudflare"), None
            ])
            await wait_for_manual_cloudflare(
                adapter=adapter, page=object(), member_label="Membro #42",
                professional_name="Brenda",
            )
        solve.assert_awaited_once()
        self.assertIn(
            "2Captcha não concluiu (TURNSTILE_NOT_CAPTURED)",
            send.await_args.args[0],
        )

    @patch("app.comment_automation.discord_manual_gate.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.send_discord", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.solve_cloudflare_turnstile", new_callable=AsyncMock)
    async def test_missing_api_key_reports_reason_in_manual_fallback(self, solve, send, sleep):
        from types import SimpleNamespace
        with patch("app.comment_automation.discord_manual_gate.settings",
                   SimpleNamespace(twocaptcha_api_key="", comment_manual_wait_seconds=60)):
            adapter = FakeAdapter([
                ManualIntervention("Proteção/verificação detectada: cloudflare"), None
            ])
            await wait_for_manual_cloudflare(
                adapter=adapter, page=object(), member_label="Membro #42",
                professional_name="Brenda",
            )
        solve.assert_not_awaited()
        self.assertIn(
            "2Captcha não concluiu (API_KEY_NOT_CONFIGURED)",
            send.await_args.args[0],
        )


    @patch("app.comment_automation.discord_manual_gate.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.send_discord", new_callable=AsyncMock)
    @patch("app.comment_automation.discord_manual_gate.solve_cloudflare_turnstile", new_callable=AsyncMock, return_value=False)
    async def test_manual_mode_skips_solver_and_waits_for_user(self, solve, send, sleep):
        from types import SimpleNamespace
        with patch("app.comment_automation.discord_manual_gate.settings",
                   SimpleNamespace(
                       twocaptcha_api_key="test-key",
                       comment_manual_wait_seconds=60,
                       comment_captcha_mode="manual",
                   )):
            adapter = FakeAdapter([
                ManualIntervention("Proteção/verificação detectada: cloudflare"), None
            ])
            await wait_for_manual_cloudflare(
                adapter=adapter, page=object(), member_label="Membro #42",
                professional_name="Brenda",
            )
        solve.assert_not_awaited()
        send.assert_awaited_once()
        self.assertNotIn("2Captcha não concluiu", send.await_args.args[0])
        self.assertEqual(adapter.calls, 2)

if __name__ == "__main__":
    unittest.main()
