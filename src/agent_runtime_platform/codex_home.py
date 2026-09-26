"""Isolated, persistent Codex home that carries authentication without user config."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def prepare_codex_home(
    *,
    app_home: str | Path | None = None,
    source_home: str | Path | None = None,
) -> Path:
    """Copy only auth.json into a private app home; never inherit Codex config."""
    source = Path(source_home or os.getenv("CODEX_HOME") or (Path.home() / ".codex"))
    destination = Path(
        app_home
        or os.getenv("AGENT_RUNTIME_CODEX_HOME")
        or (Path.home() / ".agent-runtime-platform" / "codex-home")
    )
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    source_auth = source / "auth.json"
    target_auth = destination / "auth.json"
    if source_auth.is_file() and (
        not target_auth.exists()
        or source_auth.stat().st_mtime_ns > target_auth.stat().st_mtime_ns
    ):
        fd, temporary_name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=destination)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as target:
                with source_auth.open("rb") as source_file:
                    shutil.copyfileobj(source_file, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, target_auth)
        finally:
            temporary.unlink(missing_ok=True)
    if target_auth.exists():
        target_auth.chmod(0o600)
    return destination
