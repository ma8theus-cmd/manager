import unittest
from pathlib import Path

ROOT = Path("/home/matheus/meslibertines_manager_v1")


class MaintenanceAlignmentTests(unittest.TestCase):
    def test_dashboard_uses_current_scout_schedule(self):
        source = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Primeira checagem: +6h · segunda: +8h · máximo: 2 checagens.", source)
        self.assertNotIn("Primeira checagem: aproximadamente +1h", source)
        self.assertNotIn("máximo: 8 checagens", source)

    def test_runtime_service_has_no_retired_batch_fields(self):
        source = (ROOT / "app/services.py").read_text(encoding="utf-8")
        for retired in (
            "ACTIVE_DAILY_STATUSES",
            '"next_batch_at"',
            '"remaining_seconds"',
            '"direct_used_today"',
            '"direct_cooldown_ok"',
            '"vpn_used_today"',
        ):
            self.assertNotIn(retired, source)

    def test_rate_limit_copy_matches_no_manager_timer(self):
        service = (ROOT / "app/services.py").read_text(encoding="utf-8")
        preparer = (ROOT / "app/automation/registration_preparer.py").read_text(encoding="utf-8")
        self.assertNotIn("configured batch cooldown", service)
        self.assertNotIn("cooldown preservado", preparer)
        self.assertIn("histórico", service)

    def test_comment_scheduler_has_no_dead_planning_stub(self):
        source = (ROOT / "app/comment_automation/service.py").read_text(encoding="utf-8")
        self.assertNotIn("def _plan_today", source)
        self.assertNotIn("_plan_today()", source)

    def test_env_example_has_current_batch_and_scout_settings(self):
        source = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("REGISTRATION_DIRECT_LIMIT=5", source)
        self.assertIn("REGISTRATION_VPN_DAILY_LIMIT=5", source)
        self.assertNotIn("DAILY_ACCOUNT_LIMIT", source)
        self.assertNotIn("SCOUT_CHECK_INTERVAL_MINUTES", source)
        self.assertNotIn("SCOUT_MAX_CHECKS=4", source)

    def test_offline_diagnostics_are_self_contained(self):
        check = (ROOT / "scripts/system_check.py").read_text(encoding="utf-8")
        panel = (ROOT / "scripts/self_test_scout_panel.py").read_text(encoding="utf-8")
        self.assertIn("headless=True", check)
        self.assertIn('data["promising"]', panel)
        self.assertIn('data["lead_exports"]', panel)

    def test_obsolete_policy_test_is_not_active(self):
        self.assertFalse((ROOT / "scripts/test_policy_8_and_research.py").exists())

    def test_readme_describes_current_operation(self):
        source = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("REGISTRATION_DIRECT_LIMIT=5", source)
        self.assertIn("REGISTRATION_VPN_DAILY_LIMIT=5", source)
        self.assertNotIn("DAILY_ACCOUNT_LIMIT", source)
        self.assertIn("sem temporizador do Manager", source)
        self.assertIn("+6h e +8h", source)
        self.assertNotIn("não marca os termos", source)


if __name__ == "__main__":
    unittest.main()
