"""Regenerate contracts/openapi.json from the FastAPI application.

Usage: python scripts/export_openapi.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_runtime_platform.openapi_contract import contract_drift, write_contract  # noqa: E402


def main() -> int:
    path = write_contract()
    print(f"wrote {path.relative_to(ROOT)}")
    drift = contract_drift()
    if drift is not None:
        print(drift)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
