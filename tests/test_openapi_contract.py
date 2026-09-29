from __future__ import annotations

import json

from agent_runtime_platform.api.openapi_contract import CONTRACT_PATH, contract_drift


def test_committed_contract_matches_the_application():
    drift = contract_drift()
    assert drift is None, drift


def test_contract_is_a_usable_openapi_document():
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert document["openapi"].startswith("3.")
    assert document["info"]["title"]
    assert document["paths"], "the contract must describe at least one route"
    for path, methods in document["paths"].items():
        assert path.startswith("/"), path
        assert methods, path
