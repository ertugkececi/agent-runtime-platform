"""The exported OpenAPI contract.

``contracts/openapi.json`` is generated from the FastAPI application and is the
single source of truth for the HTTP interface. Consumers generate clients from
it; nobody edits it by hand and no second copy of the schema is maintained.

The file is committed so a change to the API surface is visible in review. A
test regenerates it and fails when the committed copy has drifted. Regenerate
with ``python scripts/export_openapi.py``.

JSON is FastAPI's native format and needs no extra dependency. Serialisation is
stable (sorted keys, two-space indent) so the drift check compares bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "contracts" / "openapi.json"


def current_contract() -> dict:
    """Build the OpenAPI document from the application as it is now."""
    from agent_runtime_platform.api.app import create_app

    app = create_app(database_url="sqlite:///:memory:")
    try:
        return app.openapi()
    finally:
        app.state.database.dispose()


def serialized_contract(contract: dict | None = None) -> str:
    """Return the canonical on-disk representation of the contract."""
    document = current_contract() if contract is None else contract
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_contract() -> Path:
    """Write the current contract to disk and return its path."""
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(serialized_contract(), encoding="utf-8", newline="\n")
    return CONTRACT_PATH


def contract_drift() -> str | None:
    """Return a human-readable reason the committed contract is stale, or None."""
    if not CONTRACT_PATH.is_file():
        return f"{CONTRACT_PATH.name} is missing; run: python scripts/export_openapi.py"
    if CONTRACT_PATH.read_text(encoding="utf-8") != serialized_contract():
        return "contracts/openapi.json is out of date; run: python scripts/export_openapi.py"
    return None
