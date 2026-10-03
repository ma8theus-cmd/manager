from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import settings


class WorkerClientError(RuntimeError):
    pass


_ALLOWED_MEMBER_FIELDS = {
    "id",
    "first_name",
    "last_name",
    "birth_date",
    "city_france",
    "username",
    "email",
}


def build_job_payload(job_id: str, action: str, route: str, member: dict[str, Any]) -> dict[str, Any]:
    if not job_id or action not in {"create_account", "activate_profile"}:
        raise WorkerClientError("Trabalho remoto inválido.")
    if route not in {"DIRECT", "VPN"}:
        raise WorkerClientError("Rota remota inválida.")
    safe_member = {key: member.get(key) for key in _ALLOWED_MEMBER_FIELDS}
    return {
        "job_id": str(job_id),
        "action": action,
        "route": route,
        "member": safe_member,
    }


def worker_is_configured() -> bool:
    return bool(settings.worker_control_url and settings.worker_api_token)
def _request(
    base_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not base_url or not token:
        raise WorkerClientError("Worker secundário não está configurado.")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + "/" + path.lstrip("/"),
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Worker-Token": token,
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=settings.worker_request_timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raise WorkerClientError(f"Worker secundário recusou a requisição (HTTP {exc.code}).") from exc
    except URLError as exc:
        raise WorkerClientError("Worker secundário indisponível.") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WorkerClientError("Worker secundário retornou JSON inválido.") from exc
    if not isinstance(data, dict):
        raise WorkerClientError("Worker secundário retornou formato inválido.")
    return data


def submit_job(
    base_url: str,
    token: str,
    job_id: str,
    action: str,
    route: str,
    member: dict[str, Any],
) -> dict[str, Any]:
    if action != "create_account":
        raise WorkerClientError(
            "A VPS2 recebe somente criação de contas; ativação de perfil ocorre na VPS principal."
        )
    payload = build_job_payload(job_id, action, route, member)
    return _request(base_url, token, "/v1/jobs", method="POST", payload=payload)


def get_job_status(base_url: str, token: str, job_id: str) -> dict[str, Any]:
    return _request(base_url, token, f"/v1/jobs/{quote(str(job_id), safe='')}")


def worker_capabilities() -> dict[str, Any]:
    return _request(
        settings.worker_control_url,
        settings.worker_api_token,
        "/v1/capabilities",
    )


def worker_ready() -> bool:
    if not worker_is_configured():
        return False
    try:
        capabilities = worker_capabilities()
    except WorkerClientError:
        return False
    return bool(capabilities.get("ok")) and {"DIRECT", "VPN"} <= set(capabilities.get("routes", []))


def worker_vpn_ready() -> bool:
    if not worker_is_configured():
        return False
    try:
        capabilities = worker_capabilities()
    except WorkerClientError:
        return False
    return worker_ready_from_capabilities(capabilities) and bool(capabilities.get("vpn_ready"))


def worker_ready_from_capabilities(capabilities: dict[str, Any]) -> bool:
    return bool(capabilities.get("ok")) and {"DIRECT", "VPN"} <= set(capabilities.get("routes", []))
