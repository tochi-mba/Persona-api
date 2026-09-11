"""Shared fixtures.

Every test that touches the app builds its own, so nothing leaks between cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from persona_api.core.config import LogFormat, Settings
from persona_api.storage.database import Database
from persona_api.storage.migrator import migrate
from tests.fakes.clock import EPOCH

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

KEYRING_ISSUER = "https://keyring.test"
JWKS_URL = "https://keyring.test/.well-known/jwks.json"
AUDIENCE = "persona"

ACCOUNT = "acct_one"
OTHER_ACCOUNT = "acct_two"
PROFILE = "work"


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Test settings, built through validation.

    Overrides go through the constructor rather than ``model_copy(update=...)``, which
    skips validators -- so a path would stay unresolved and mean two places after a
    chdir.
    """
    defaults: dict[str, Any] = {
        "_env_file": None,
        "database_path": tmp_path / "persona.db",
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": KEYRING_ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "audience": AUDIENCE,
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """A migrated database on a real file.

    A real file rather than ``:memory:`` on purpose: durability is the property this
    storage exists for, so the tests exercise the journal mode a deployment actually
    runs on -- and an in-memory database would be a fresh one per connection, which is
    exactly the thing the restart tests are trying to disprove.
    """
    db = Database(tmp_path / "persona.db")
    migrate(db, now=EPOCH)
    try:
        yield db
    finally:
        await db.aclose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a scratch directory."""
    return build_settings(tmp_path)
