"""The smoke script's environment can sit beside the service's own.

``make smoke`` is run from a shell that is usually the one that just started persona-api,
or a sibling of it. Every ``PERSONA_``-prefixed variable the service does not know is
a startup error, so a smoke variable carrying that prefix turns "export what the smoke run
needs" into "the service no longer starts". The test reads the names out of the script
rather than repeating them, so a new variable is checked the day it is added.
"""

from __future__ import annotations

import ast
from pathlib import Path

from persona_api.core.config import check_for_unknown_env_vars

SMOKE = Path(__file__).resolve().parents[2] / "scripts" / "smoke.py"


def _variables_read() -> set[str]:
    """Every name the script passes to ``os.environ.get``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(SMOKE.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and ast.unparse(node.func.value) == "os.environ"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def test_the_script_reads_its_own_url_under_a_smoke_prefix() -> None:
    assert "SMOKE_PERSONA_API_URL" in _variables_read()


def test_every_smoke_variable_can_be_exported_where_the_service_starts() -> None:
    names = _variables_read()
    assert names, "found no os.environ.get calls in scripts/smoke.py"
    check_for_unknown_env_vars(dict.fromkeys(names, "set-for-the-smoke-run"))
