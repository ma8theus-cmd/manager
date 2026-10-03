import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.discord_notifier import (
    enqueue_notification,
    ensure_notification_schema,
    list_due_notifications,
    mark_notification_failed,
)
from app.worker_client import WorkerClientError, build_job_payload, submit_job


MEMBER = {
    "id": 7,
    "first_name": "Jean",
    "last_name": "Dupont",
    "birth_date": "1990-01-02",
    "city_france": "Paris",
    "username": "jean.dupont",
    "email": "jean7@batutus.site",
}


class WorkerClientTests(unittest.TestCase):
    def test_payload_contains_no_password_or_secret(self):
        payload = build_job_payload("job-7", "create_account", "DIRECT", MEMBER)
        self.assertEqual(payload["route"], "DIRECT")
        self.assertNotIn("password", payload)
        self.assertNotIn("secret", str(payload).lower())

    def test_profile_activation_is_not_submitted_to_secondary_worker(self):
        with self.assertRaises(WorkerClientError):
            submit_job(
                "http://worker.test",
                "token",
                "job-profile-7",
                "activate_profile",
                "DIRECT",
                MEMBER,
            )

    @patch("app.main.launch_profile_activation")
    @patch("app.main.mark_email_confirmed")
    @patch("app.main.get_members_by_ids")
    def test_email_confirmation_always_starts_primary_activation(
        self,
        get_members_by_ids,
        mark_email_confirmed,
        launch_profile_activation,
    ):
        from app.main import email_confirmed

        get_members_by_ids.return_value = [{
            "id": 7,
            "registration_target": "SECONDARY",
            "email_status": "CONFIRMED",
            "profile_status": "PENDING",
        }]
        response = email_confirmed(7, SimpleNamespace(headers={}))
        launch_profile_activation.assert_called_once_with([7])
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("VPS2", response.headers["location"])

    def test_account_notification_queue_is_idempotent_and_retriable(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_notification_schema(conn)

        self.assertTrue(
            enqueue_notification(
                conn,
                "account-created:job-7",
                "account 7",
                now="2026-09-20T00:00:00+00:00",
            )
        )
        self.assertFalse(
            enqueue_notification(
                conn,
                "account-created:job-7",
                "account 7",
                now="2026-09-20T00:00:01+00:00",
            )
        )
        pending = list_due_notifications(conn, "2026-09-20T00:00:02+00:00")
        self.assertEqual(len(pending), 1)

        retry_at = mark_notification_failed(
            conn,
            pending[0]["id"],
            "HTTP 429",
            now="2026-09-20T00:00:02+00:00",
        )
        self.assertEqual(list_due_notifications(conn, "2026-09-20T00:00:02+00:00"), [])
        self.assertEqual(len(list_due_notifications(conn, retry_at)), 1)

    @patch("app.worker_client.urlopen")
    def test_submit_sends_authenticated_json(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"status":"QUEUED","job_id":"job-7"}'
        response.status = 202
        result = submit_job("http://worker.test", "token", "job-7", "create_account", "DIRECT", MEMBER)
        self.assertEqual(result["job_id"], "job-7")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-worker-token"), "token")
        self.assertNotIn("password", request.data.decode().lower())


if __name__ == "__main__":
    unittest.main()
