import unittest

from app.comment_automation.live_challenge import (
    clear_session,
    create_session,
    forward_click,
    screenshot_for_session,
    session_is_valid,
)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.screenshot_calls = 0
        self.fail_screenshot = False

    async def screenshot(self, **kwargs):
        self.screenshot_calls += 1
        if self.fail_screenshot:
            raise RuntimeError("page is navigating")
        return b"jpeg-frame"

    async def evaluate(self, _script):
        return {"width": 1280, "height": 720}


class LiveChallengeViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        clear_session()

    async def test_session_code_limits_frame_and_click_access(self):
        page = FakePage()
        code = create_session(page, now=100, ttl_seconds=30)
        self.assertIsNone(await screenshot_for_session("wrong-code", now=101))
        self.assertEqual(await screenshot_for_session(code, now=101), b"jpeg-frame")
        self.assertTrue(await forward_click(code, 320, 180, now=101))
        self.assertEqual(page.mouse.clicks, [(320, 180)])
        self.assertFalse(await forward_click(code, 1280, 180, now=101))
        self.assertFalse(await forward_click(code, 320, 180, now=131))

    async def test_temporary_screenshot_failure_keeps_session_active(self):
        page = FakePage()
        code = create_session(page, now=100, ttl_seconds=30)
        page.fail_screenshot = True
        self.assertIsNone(await screenshot_for_session(code, now=101))
        self.assertTrue(session_is_valid(code, now=101))
        self.assertFalse(session_is_valid(code, now=131))

    async def test_session_expires_and_can_be_closed(self):
        code = create_session(FakePage(), now=100, ttl_seconds=10)
        self.assertIsNone(await screenshot_for_session(code, now=111))
        clear_session(code)
        self.assertIsNone(await screenshot_for_session(code, now=101))


if __name__ == "__main__":
    unittest.main()
