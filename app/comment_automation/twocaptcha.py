from __future__ import annotations

import asyncio
import time
from typing import Any

API_URL = "https://api.2captcha.com"


class TwoCaptchaApiError(RuntimeError):
    """Sanitized 2Captcha failure; never includes request credentials."""


def build_challenge_task(challenge: dict[str, Any]) -> dict[str, str]:
    required = {
        "websiteURL": challenge.get("websiteURL"),
        "websiteKey": challenge.get("sitekey"),
        "action": challenge.get("action"),
        "data": challenge.get("data"),
        "pagedata": challenge.get("pagedata"),
    }
    if any(not isinstance(value, str) or not value for value in required.values()):
        raise TwoCaptchaApiError("CHALLENGE_PARAMETERS_MISSING")
    task = {"type": "TurnstileTaskProxyless", **required}
    user_agent = challenge.get("userAgent")
    if isinstance(user_agent, str) and user_agent:
        task["userAgent"] = user_agent
    return task


async def solve_task(
    request, api_key: str, task: dict[str, str], *,
    poll_interval: float = 5, max_wait_seconds: float = 180,
) -> dict[str, str]:
    if not api_key:
        raise TwoCaptchaApiError("API_KEY_NOT_CONFIGURED")
    timeout_ms = 20_000
    response = await request.post(
        f"{API_URL}/createTask",
        data={"clientKey": api_key, "task": task}, timeout=timeout_ms,
    )
    if not response.ok:
        raise TwoCaptchaApiError("CREATE_TASK_HTTP_ERROR")
    created = await response.json()
    if created.get("errorId"):
        raise TwoCaptchaApiError(str(created.get("errorCode") or "CREATE_TASK_FAILED"))
    task_id = created.get("taskId")
    if not task_id:
        raise TwoCaptchaApiError("TASK_ID_MISSING")

    deadline = time.monotonic() + max(0, max_wait_seconds)
    while time.monotonic() < deadline:
        await asyncio.sleep(max(0, poll_interval))
        response = await request.post(
            f"{API_URL}/getTaskResult",
            data={"clientKey": api_key, "taskId": task_id},
            timeout=timeout_ms,
        )
        if not response.ok:
            raise TwoCaptchaApiError("GET_RESULT_HTTP_ERROR")
        result = await response.json()
        if result.get("errorId"):
            raise TwoCaptchaApiError(str(result.get("errorCode") or "SOLVE_FAILED"))
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            if not solution.get("token"):
                raise TwoCaptchaApiError("SOLUTION_TOKEN_MISSING")
            return solution
    raise TwoCaptchaApiError("SOLVE_TIMEOUT")


TURNSTILE_INTERCEPTOR = r"""
(() => {
  const state = window.__managerTwoCaptcha = {
    challenge: null, callback: null, nativeRender: null, renderArgs: null,
  };
  const install = () => {
    const api = window.turnstile;
    if (!api || typeof api.render !== "function" || api.render.__managerTwoCaptcha) return;
    const original = api.render.bind(api);
    const wrapped = (container, options) => {
      if (options && options.sitekey && options.cData && options.chlPageData) {
        state.challenge = {
          sitekey: options.sitekey, websiteURL: location.href,
          action: options.action || "", data: options.cData,
          pagedata: options.chlPageData, userAgent: navigator.userAgent,
        };
        state.callback = options.callback;
        state.nativeRender = original;
        state.renderArgs = [container, options];
        return "manager-2captcha";
      }
      return original(container, options);
    };
    wrapped.__managerTwoCaptcha = true;
    api.render = wrapped;
  };
  setInterval(install, 10);
})();
"""


async def install_turnstile_interceptor(context) -> None:
    await context.add_init_script(TURNSTILE_INTERCEPTOR)


async def capture_challenge(page, timeout_ms: int = 15_000) -> dict[str, Any] | None:
    try:
        handle = await page.wait_for_function(
            "() => Boolean(window.__managerTwoCaptcha?.challenge)",
            timeout=timeout_ms,
        )
        return await handle.json_value()
    except Exception:
        return None
async def restore_native_turnstile(page) -> bool:
    try:
        return bool(await page.evaluate("""() => {
          const state = window.__managerTwoCaptcha;
          if (!state || !state.nativeRender || !state.renderArgs) return false;
          state.nativeRender(...state.renderArgs);
          state.nativeRender = null;
          state.renderArgs = null;
          return true;
        }"""))
    except Exception:
        return False


async def apply_solution(page, solution: dict[str, str]) -> bool:
    user_agent = solution.get("userAgent")
    if user_agent:
        session = await page.context.new_cdp_session(page)
        try:
            await session.send(
                "Network.setUserAgentOverride", {"userAgent": user_agent}
            )
        finally:
            await session.detach()
    return bool(await page.evaluate("""token => {
      const callback = window.__managerTwoCaptcha?.callback;
      if (typeof callback !== "function") return false;
      callback(token);
      return true;
    }""", solution["token"]))


async def solve_cloudflare_turnstile(
    page, api_key: str, *, poll_interval: float = 5,
    max_wait_seconds: float = 180,
) -> bool:
    challenge = await capture_challenge(page)
    if challenge is None:
        return False
    try:
        task = build_challenge_task(challenge)
        solution = await solve_task(
            page.context.request, api_key, task,
            poll_interval=poll_interval, max_wait_seconds=max_wait_seconds,
        )
        if not await apply_solution(page, solution):
            raise TwoCaptchaApiError("PAGE_CALLBACK_MISSING")
        return True
    except Exception:
        await restore_native_turnstile(page)
        raise
