"""The composition root.

Every adapter is chosen and wired here, once, and handed to the app. Nothing else
constructs its own dependencies -- which is what makes the whole service testable by
substitution, and what keeps "which store" and "which clock" configuration decisions
rather than code.

Much smaller than keyring's, and the reason is ADR-0003: there is no administrative
surface, so there is no role store, no audit log of privileged actions, no break-glass
actor and no permission resolver to wire. What is left is three stores, an event log, a
JWKS client and a verifier.

There is also **no background sweeper**. keyring has one because sessions, grants and
rate-limit records expire; nothing here does. A forgotten field is a tombstone somebody
may still ask for, and the event log is trimmed inside the transaction that writes it --
so there is nothing for a periodic task to collect, and no task whose silent death would
let something grow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_api.auth.jwks import JwksClient
from persona_api.auth.verifier import TokenVerifier
from persona_api.core.clock import SystemClock
from persona_api.core.logging import get_logger
from persona_api.core.preferences import build_preference_source
from persona_api.domain.fields import ValueLimits
from persona_api.events.sql_log import SqlEventLog
from persona_api.memory.fields import FieldStore
from persona_api.memory.notes import NoteStore
from persona_api.personas.service import PersonaService
from persona_api.personas.store import PersonaStore
from persona_api.storage.database import Database
from persona_api.storage.migrator import migrate

if TYPE_CHECKING:
    from persona_api.core.clock import Clock
    from persona_api.core.config import Settings
    from persona_api.core.preferences import PreferenceSource
    from persona_api.events.log import EventLog

logger = get_logger(__name__)


@dataclass(slots=True)
class Container:
    """Everything the API needs, already wired together."""

    settings: Settings
    clock: Clock
    database: Database
    # The port, not the adapter. Annotating this with the concrete class would make
    # every consumer of the container depend on which adapter was wired, which is the
    # one thing a composition root exists to prevent.
    events: EventLog
    personas: PersonaStore
    fields: FieldStore
    notes: NoteStore
    persona_service: PersonaService
    jwks: JwksClient
    verifier: TokenVerifier
    preferences: PreferenceSource
    started_monotonic: float

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        preferences: PreferenceSource | None = None,
    ) -> Container:
        """Construct every adapter named by ``settings``.

        Args:
            settings: the configuration to wire.
            clock: substituted by tests that need to control time.
            preferences: substituted by tests, which read people's settings from a fake
                settings-api rather than a real one.
        """
        clock = clock or SystemClock()
        database = Database(settings.database_path)
        # Everything from here on can refuse to start -- a migration that fails, a
        # policy file that says something the catalogue rejects -- and a refusal
        # must not leave the database it just opened for the garbage collector.
        try:
            migrate(database, now=clock.now())

            events = SqlEventLog(database=database, clock=clock, max_entries=settings.max_events)
            personas = PersonaStore(database=database)
            fields = FieldStore(
                database=database,
                clock=clock,
                events=events,
                limits=ValueLimits(
                    max_bytes=settings.max_field_value_bytes,
                    max_depth=settings.max_value_depth,
                    max_list_items=settings.max_value_list_items,
                    max_object_keys=settings.max_value_object_keys,
                ),
                max_fields=settings.max_fields_per_persona,
                max_pinned=settings.max_pinned_fields,
            )
            notes = NoteStore(
                database=database,
                clock=clock,
                events=events,
                max_body_chars=settings.max_note_body_chars,
                max_notes=settings.max_notes_per_persona,
                max_pinned=settings.max_pinned_notes,
            )
            # Constructed, not contacted. The first token that needs a key, or the first health
            # check, is what provokes the first fetch; a persona service that will not start
            # because keyring is down is a persona service that cannot report keyring being down.
            jwks = JwksClient(
                url=settings.keyring_jwks_url,
                clock=clock,
                cache_seconds=settings.jwks_cache_seconds,
                min_refetch_seconds=settings.jwks_min_refetch_seconds,
                timeout_seconds=settings.keyring_http_timeout_seconds,
                # The shared client's diagnostics -- a refused key id, a fetch that failed -- land
                # in this service's structured, redacted log rather than the standard library's.
                logger=get_logger("persona_api.auth.jwks"),
            )
            # Same rule for settings-api: constructed here, contacted on the first request that
            # needs somebody's own caps. An empty URL keeps today's behaviour exactly.
            chosen = preferences if preferences is not None else build_preference_source(settings)

            return cls(
                settings=settings,
                clock=clock,
                database=database,
                events=events,
                personas=personas,
                fields=fields,
                notes=notes,
                persona_service=PersonaService(
                    personas=personas,
                    fields=fields,
                    notes=notes,
                    events=events,
                    clock=clock,
                    settings=settings,
                ),
                jwks=jwks,
                verifier=TokenVerifier(
                    jwks=jwks,
                    issuer=settings.keyring_issuer,
                    audience=settings.audience,
                    clock=clock,
                ),
                preferences=chosen,
                started_monotonic=clock.monotonic(),
            )
        except BaseException:
            database.close()
            raise

    @property
    def uptime_seconds(self) -> float:
        return self.clock.monotonic() - self.started_monotonic

    async def aclose(self) -> None:
        """Shut everything down in dependency order."""
        try:
            await self.preferences.aclose()
            await self.jwks.aclose()
        finally:
            # Last: everything above may still want to write on its way out. And in a
            # `finally`, because a close above that raises must not leave the database
            # open -- an unclosed connection outlives the error that caused it.
            await self.database.aclose()
