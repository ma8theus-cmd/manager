import fcntl
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import get_pending_profile_activation_ids


class PendingProfileActivationTests(unittest.TestCase):
    def test_returns_only_retryable_confirmed_profiles_in_id_order(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE members (id INTEGER, email_status TEXT, profile_status TEXT, registration_status TEXT)"
        )
        conn.executemany(
            "INSERT INTO members VALUES (?, ?, ?, ?)",
            [
                (3, "CONFIRMED", "PENDING", "EMAIL_CONFIRMED"),
                (1, "CONFIRMED", "MANUAL_INTERVENTION", "EMAIL_CONFIRMED"),
                (2, "CONFIRMED", "ACTIVATING", "PROFILE_PENDING"),
                (4, "PENDING", "PENDING", "EMAIL_CONFIRMED"),
                (5, "CONFIRMED", "PENDING", "WAITING_EMAIL_CONFIRMATION"),
            ],
        )
        try:
            with patch("app.services.get_connection", return_value=conn):
                self.assertEqual(get_pending_profile_activation_ids(), [1, 3])
        finally:
            conn.close()


class ProfileActivationLockTests(unittest.TestCase):
    def test_lock_reports_running_worker_and_releases_cleanly(self):
        from app.profile_activation import profile_activation_is_running

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "profile.lock"
            with patch("app.profile_activation._profile_lock_path", return_value=lock_path):
                with lock_path.open("a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertTrue(profile_activation_is_running())
                    fcntl.flock(handle, fcntl.LOCK_UN)
                self.assertFalse(profile_activation_is_running())


class DashboardBatchActivationTests(unittest.TestCase):
    def test_panel_exposes_batch_activation_action(self):
        template = Path("/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html").read_text()
        self.assertIn('action="/members/activate-pending-profiles"', template)
        self.assertIn("Ativar todos os perfis pendentes", template)


class ProfileActivationLaunchTests(unittest.TestCase):
    def test_launcher_passes_unique_sorted_ids_to_official_worker(self):
        from app import profile_activation

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_settings = SimpleNamespace(logs_dir=root, project_root=root)
            fake_process = Mock(pid=4321)
            with patch.object(profile_activation, "settings", fake_settings), \
                 patch.object(profile_activation, "_profile_lock_path", return_value=root / "profile.lock"), \
                 patch.object(profile_activation.subprocess, "Popen", return_value=fake_process) as popen:
                self.assertEqual(profile_activation.launch_profile_activation([3, 1, 3]), 4321)

            command = popen.call_args.args[0]
            self.assertEqual(command[2:], ["--ids", "1", "3"])
            self.assertEqual(popen.call_args.kwargs["start_new_session"], True)
            self.assertIn("DISPLAY", popen.call_args.kwargs["env"])


class BatchRouteTests(unittest.TestCase):
    def test_batch_route_launches_current_pending_queue(self):
        from app import main

        with patch.object(main, "get_pending_profile_activation_ids", return_value=[7, 9]), \
             patch.object(main, "launch_profile_activation", return_value=9876) as launch:
            response = main.activate_pending_profiles()

        self.assertEqual(response.status_code, 303)
        launch.assert_called_once_with([7, 9])


if __name__ == "__main__":
    unittest.main()
