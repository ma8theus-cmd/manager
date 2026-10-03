import unittest
from app.comment_automation.twocaptcha import (
    TwoCaptchaApiError,
    build_challenge_task,
    solve_task,
)


class Response:
    def __init__(self, payload, ok=True):
        self.payload = payload
        self.ok = ok

    async def json(self):
        return self.payload


class Request:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, *, data, timeout):
        self.calls.append((url, data, timeout))
        return self.responses.pop(0)


class TwoCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def test_cloudflare_task_has_only_required_challenge_fields(self):
        task = build_challenge_task({
            "sitekey": "site-key", "websiteURL": "https://example.test/check",
            "action": "managed", "data": "cdata", "pagedata": "page-data",
            "userAgent": "Chrome UA",
        })
        self.assertEqual(task, {
            "type": "TurnstileTaskProxyless",
            "websiteURL": "https://example.test/check", "websiteKey": "site-key",
            "action": "managed", "data": "cdata", "pagedata": "page-data",
            "userAgent": "Chrome UA",
        })

    async def test_submits_task_polls_and_returns_solution(self):
        request = Request([
            Response({"errorId": 0, "taskId": 123}),
            Response({"errorId": 0, "status": "ready", "solution": {
                "token": "solved-token", "userAgent": "solver UA"
            }}),
        ])
        result = await solve_task(request, "test-secret", {"type": "TurnstileTaskProxyless"},
                                  poll_interval=0, max_wait_seconds=5)
        self.assertEqual(result["token"], "solved-token")
        self.assertEqual(request.calls[0][1]["clientKey"], "test-secret")
        self.assertEqual(request.calls[1][1]["taskId"], 123)

    async def test_api_error_does_not_echo_api_key(self):
        request = Request([Response({"errorId": 1, "errorCode": "ERROR_KEY"})])
        with self.assertRaises(TwoCaptchaApiError) as raised:
            await solve_task(request, "test-secret", {"type": "TurnstileTaskProxyless"})
        self.assertNotIn("test-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
