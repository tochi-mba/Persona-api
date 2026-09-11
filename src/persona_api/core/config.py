"""Application configuration.

Every knob is an environment variable prefixed ``PERSONA_``; nested settings would use a
double underscore, though there are none yet. Unknown variables under the prefix are
rejected rather than ignored (see :func:`check_for_unknown_env_vars`), so a typo in a
deployment surfaces at startup instead of silently leaving a security-relevant default
in place.

**The deliberate absences are the interesting part of this module**, and they are listed
here because a setting that does not exist cannot be turned on by somebody in a hurry:

* There is no setting that disables token verification.
* There is no setting that lets one account read another's persona. There is no admin
  surface for one to enable -- see ``docs/adr/0003-no-administrative-surface.md``.
* There is no setting that turns off the credential refusal. People will paste an API
  key into "remember this", and the answer is always the vault next door.
* There is no setting that permits an unsigned token, or ``HS256``, or any algorithm but
  the one pinned in :mod:`persona_api.auth.verifier`.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "PERSONA_"
ENV_NESTED_DELIMITER = "__"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # -- Identity ----------------------------------------------------------------------
    app_name: str = "persona"
    environment: str = "local"

    # -- Observability -----------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Serving -----------------------------------------------------------------------
    host: str = "127.0.0.1"
    """Loopback by default. This service belongs behind a TLS-terminating proxy."""

    port: PositiveInt = 8002

    # -- Storage -----------------------------------------------------------------------
    database_path: Path = Path("var/persona.db")
    """The one file everything lives in. Nothing in it is encrypted.

    Kept at mode 0600 by :func:`persona_api.storage.database.make_private`, which is the
    only thing standing between a persona and every other process on the machine.
    """

    # -- Authenticating against keyring ------------------------------------------------
    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    """Where keyring publishes the public half of its signing key.

    Fetched lazily, on the first token that needs it -- never at startup. A persona
    service that will not start because keyring is down is a persona service that cannot
    report keyring being down.
    """

    keyring_issuer: str = "http://127.0.0.1:8001"
    """Pinned against the token's ``iss``. Must match keyring's ``KEYRING_ISSUER``."""

    audience: str = "persona"
    """Pinned against the token's ``aud``.

    A token minted for another service is refused here even though it is perfectly valid
    there -- which is the entire point of the claim. Ask keyring for one with
    ``{"audience": "persona"}``.
    """

    jwks_cache_seconds: PositiveFloat = 3_600.0
    """How long a fetched key set is trusted before it is fetched again."""

    jwks_min_refetch_seconds: PositiveFloat = 60.0
    """The floor between two refetches provoked by an unknown ``kid``.

    Load-bearing, and not a performance tuning knob. Without it, anyone can force one
    outbound fetch per request by sending tokens with random ``kid`` headers -- a DoS
    amplifier with this service's credentials on it, pointed at keyring.
    """

    keyring_http_timeout_seconds: PositiveFloat = 5.0

    # -- Limits ------------------------------------------------------------------------
    # Every one of these is enforced *inside the transaction that does the write*, never
    # by a caller that counts first. A count taken before a write goes stale between the
    # two, and two concurrent writes both pass it. See AGENTS.md.
    max_personas_per_account: PositiveInt = 20
    max_fields_per_persona: PositiveInt = 500
    max_notes_per_persona: PositiveInt = 5_000

    max_pinned_fields: PositiveInt = 20
    max_pinned_notes: PositiveInt = 20
    """Pinned is a token budget, not a preference.

    Every pinned entry goes into the assistant's prompt on every turn, so the cap is
    really "how much of the context window may a persona spend on itself".
    """

    max_field_value_bytes: PositiveInt = 4_096
    max_value_depth: PositiveInt = 3
    max_value_list_items: PositiveInt = 100
    max_value_object_keys: PositiveInt = 50
    max_note_body_chars: PositiveInt = 4_000

    max_events: PositiveInt = 10_000
    """The event log is capped and trimmed in the same transaction as the insert.

    An uncapped table driven by write volume grows without anybody deciding it should.
    """

    recall_default_limit: PositiveInt = 20
    recall_max_limit: PositiveInt = 100

    @field_validator("database_path")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        """Resolve early so a relative path cannot mean two places after a chdir."""
        return value.expanduser().resolve()


class UnknownSettingError(ValueError):
    """A ``PERSONA_``-prefixed variable is set that no setting corresponds to."""


def known_env_names(model: type[BaseModel] = Settings, prefix: str = ENV_PREFIX) -> set[str]:
    """Every environment variable name this configuration understands.

    Walks nested settings models, so a nested setting added later is recognised without
    anybody remembering to extend this.
    """
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            names |= known_env_names(annotation, f"{env_name}{ENV_NESTED_DELIMITER}")
        else:
            names.add(env_name)
    return names


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Fail on a misspelled setting instead of quietly running with the default.

    pydantic-settings ignores prefixed variables it does not recognise, which for most
    services is a harmless convenience. Here it is not: ``PERSONA_AUDEINCE=persona``
    would leave the audience on its default with nothing in the logs to say so, and
    ``PERSONA_KEYRING_ISUER`` would leave the issuer unpinned against the deployment it
    is meant to be pinned to.

    Raises:
        UnknownSettingError: naming every unrecognised variable, so a deployment is
            fixed in one pass rather than one restart per typo.
    """
    present = environ if environ is not None else os.environ
    unknown = sorted(
        name for name in present if name.startswith(ENV_PREFIX) and name not in known_env_names()
    )
    if unknown:
        msg = f"unknown {ENV_PREFIX}* environment variables: {', '.join(unknown)}"
        raise UnknownSettingError(msg)


def load_settings() -> Settings:
    """Build settings from the environment and ``.env``."""
    check_for_unknown_env_vars()
    return Settings()
