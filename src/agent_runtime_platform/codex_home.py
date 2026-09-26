"""Isolated, persistent Codex home that carries authentication without user config."""
from __future__ import annotations

import os
import shutil
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
        temporary = destination / ".auth.json.tmp"
        shutil.copyfile(source_auth, temporary)
        temporary.chmod(0o600)
        os.replace(temporary, target_auth)
    if target_auth.exists():
        target_auth.chmod(0o600)
    return destination
