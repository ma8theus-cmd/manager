from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from .config import settings


class ProfileActivationAlreadyRunning(RuntimeError):
    """Raised when another profile activation worker owns the lock."""


def _profile_lock_path() -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings.logs_dir / "profile_activation.lock"


def profile_activation_is_running() -> bool:
    lock_path = _profile_lock_path()
    with lock_path.open("a+") as handle:
        acquired = False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            return True
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def _normalize_member_ids(member_ids: int | Iterable[int]) -> list[int]:
    if isinstance(member_ids, int):
        member_ids = [member_ids]

    ids = sorted({int(member_id) for member_id in member_ids})
    if not ids:
        raise ValueError("Nenhum perfil foi selecionado para ativação.")
    if any(member_id <= 0 for member_id in ids):
        raise ValueError("ID de membro inválido.")
    return ids


def launch_profile_activation(member_ids: int | Iterable[int]) -> int:
    """Start one detached worker for the supplied profile IDs."""
    ids = _normalize_member_ids(member_ids)
    lock_path = _profile_lock_path()
    lock_handle = lock_path.open("a+")
    locked = False
    spawned = False
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise ProfileActivationAlreadyRunning(
                "Já existe uma ativação de perfil em andamento."
            ) from exc

        script = settings.project_root / "scripts" / "complete_profile.py"
        log_handle = (settings.logs_dir / "profile_activation.log").open(
            "ab", buffering=0
        )
        try:
            env = os.environ.copy()
            if not env.get("DISPLAY"):
                env["DISPLAY"] = ":10.0"
            if not env.get("XAUTHORITY"):
                env["XAUTHORITY"] = str(Path.home() / ".Xauthority")
            command = [
                sys.executable,
                str(script),
                "--ids",
                *[str(member_id) for member_id in ids],
            ]
            process = subprocess.Popen(
                command,
                cwd=str(settings.project_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lock_handle.fileno(),),
            )
            spawned = True
            return process.pid
        finally:
            log_handle.close()
    finally:
        if locked and not spawned:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        # On success the child inherited this descriptor and keeps the lock.
        lock_handle.close()
