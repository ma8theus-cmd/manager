import unittest

from app.site_adapter.meslibertines import ManualIntervention
from scripts.complete_profile import run_with_controlled_retries


class ProfileActivationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_login_timeout_until_success(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ManualIntervention(
                    "Não foi possível confirmar o login dentro do tempo esperado."
                )
            return "ok"

        result = await run_with_controlled_retries(
            operation,
            max_attempts=3,
            retry_delay=0,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)

    async def test_does_not_retry_protection_intervention(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            raise ManualIntervention("Proteção/verificação detectada: captcha")

        with self.assertRaises(ManualIntervention):
            await run_with_controlled_retries(
                operation,
                max_attempts=3,
                retry_delay=0,
            )

        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
