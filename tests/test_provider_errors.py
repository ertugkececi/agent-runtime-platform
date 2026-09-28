"""Provider failures stay in one error family.

Callers distinguish a provider failure from a runtime failure by type
(``error_code = "provider_error"`` in the runtime, and the provider registry's
own handoff validation). That only holds while every provider raises
``ProviderError``, so this guard parses the provider modules and checks it,
rather than trusting convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROVIDERS = Path(__file__).resolve().parents[1] / "src" / "agent_runtime_platform" / "providers"


def _provider_modules() -> list[Path]:
    return sorted(PROVIDERS.rglob("*.py"))


def _raised_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exception = node.exc
        if isinstance(exception, ast.Call):
            exception = exception.func
        if isinstance(exception, ast.Name):
            names.append(exception.id)
        elif isinstance(exception, ast.Attribute):
            names.append(exception.attr)
        else:
            names.append(ast.dump(exception))
    return names


def test_the_guard_has_modules_to_check():
    assert _provider_modules()


def test_the_guard_actually_finds_raise_statements():
    found = sum(len(_raised_names(ast.parse(module.read_text(encoding="utf-8")))) for module in _provider_modules())
    assert found > 0, "the guard is vacuous: no raise statements were found in providers/"


@pytest.mark.parametrize("module", _provider_modules(), ids=lambda path: path.name)
def test_provider_modules_raise_only_provider_error(module: Path):
    names = _raised_names(ast.parse(module.read_text(encoding="utf-8")))
    unexpected = sorted({name for name in names if name != "ProviderError"})
    assert not unexpected, f"{module.name} raises {unexpected}; use ProviderError instead"
