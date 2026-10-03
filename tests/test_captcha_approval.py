import sqlite3
import unittest

from app.comment_automation.captcha_approval import (
    approve_captcha_approval,
    consume_captcha_approval,
    create_captcha_approval,
    ensure_captcha_approval_schema,
)


class CaptchaApprovalTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE comment_bank (id INTEGER PRIMARY KEY)")
        ensure_captcha_approval_schema(self.conn)
        self.token = create_captcha_approval(
            self.conn, 42, "brenda", "123456789", now="2026-09-30T04:00:00+00:00", ttl_seconds=120
        )

    def tearDown(self):
        self.conn.close()

    def test_only_named_discord_user_can_approve_once(self):
        self.assertFalse(approve_captcha_approval(
            self.conn, self.token, "999", now="2026-09-30T04:00:10+00:00"
        ))
        self.assertTrue(approve_captcha_approval(
            self.conn, self.token, "123456789", now="2026-09-30T04:00:11+00:00"
        ))
        self.assertFalse(approve_captcha_approval(
            self.conn, self.token, "123456789", now="2026-09-30T04:00:12+00:00"
        ))
        self.assertTrue(consume_captcha_approval(
            self.conn, self.token, now="2026-09-30T04:00:13+00:00"
        ))
        self.assertFalse(consume_captcha_approval(
            self.conn, self.token, now="2026-09-30T04:00:14+00:00"
        ))

    def test_approval_expires_at_deadline(self):
        self.assertFalse(approve_captcha_approval(
            self.conn, self.token, "123456789", now="2026-09-30T04:02:00+00:00"
        ))


if __name__ == "__main__":
    unittest.main()
