from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .config import settings


def _vpn_prefix() -> list[str]:
    value = settings.registration_vpn_exec.strip()
    return shlex.split(value) if value else []


def vpn_worker_ready() -> bool:
    """Return True only when the authorized VPN execution wrapper is usable."""
    if not settings.registration_vpn_authorized:
        return False
    prefix = _vpn_prefix()
    if not prefix:
        return False

    wrapper = Path(prefix[-1])
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        return False

    try:
        result = subprocess.run(
            [*prefix, "/usr/bin/ip", "link", "show", "tun0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def vpn_command_prefix() -> list[str]:
    if not vpn_worker_ready():
        raise RuntimeError(
            "A rota VPN autorizada não está pronta. "
            "Nenhum cadastro VPN será enviado pela rota direta."
        )
    return _vpn_prefix()
