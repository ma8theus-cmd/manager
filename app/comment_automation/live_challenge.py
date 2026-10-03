from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets
import time


@dataclass
class _ChallengeSession:
    page: object
    code_hash: str
    expires_at: float


_active: _ChallengeSession | None = None


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_session(page, *, now: float | None = None, ttl_seconds: int = 600) -> str:
    global _active
    code = secrets.token_urlsafe(18)
    instant = time.monotonic() if now is None else now
    _active = _ChallengeSession(
        page=page,
        code_hash=_hash(code),
        expires_at=instant + max(1, ttl_seconds),
    )
    return code


def _get_session(code: str, now: float | None = None) -> _ChallengeSession | None:
    global _active
    if _active is None or not code:
        return None
    instant = time.monotonic() if now is None else now
    if instant >= _active.expires_at:
        _active = None
        return None
    if not hmac.compare_digest(_active.code_hash, _hash(code)):
        return None
    return _active


def session_is_valid(code: str, now: float | None = None) -> bool:
    return _get_session(code, now) is not None


async def screenshot_for_session(
    code: str, *, now: float | None = None
) -> bytes | None:
    session = _get_session(code, now)
    if session is None:
        return None
    try:
        return await session.page.screenshot(type="jpeg", quality=65, timeout=10_000)
    except Exception:
        return None


async def forward_click(
    code: str, x: float, y: float, *, now: float | None = None
) -> bool:
    session = _get_session(code, now)
    if session is None:
        return False
    try:
        viewport = await session.page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
            return False
        await session.page.mouse.click(float(x), float(y))
        return True
    except Exception:
        return False


async def forward_wheel(
    code: str, x: float, y: float, delta_y: float, *, now: float | None = None
) -> bool:
    session = _get_session(code, now)
    if session is None:
        return False
    try:
        viewport = await session.page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
            return False
        await session.page.mouse.move(float(x), float(y))
        await session.page.mouse.wheel(0, float(delta_y))
        return True
    except Exception:
        return False


def clear_session(code: str | None = None) -> None:
    global _active
    if _active is None:
        return
    if code is None or hmac.compare_digest(_active.code_hash, _hash(code)):
        _active = None
