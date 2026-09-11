"""Free text, for what does not fit a key.

A note is the escape hatch from the structured half of a persona: "they went quiet when
I suggested rewriting it in Rust" is not a value for any key anybody would invent twice.
The rules here are therefore thin on purpose -- a body that is not empty and not
enormous, and a kind from a closed set.

**The kind is closed for the same reason a permission enum is closed.** A free-string
kind accumulates ``"lesson"``, ``"Lesson"`` and ``"lessons"``, and the note filed under
the odd one out can never be filtered for again; nobody notices until they go looking
for every lesson and one is missing.

**The body limit is passed in, not fixed here.** It is a deployment's decision -- a
prompt budget, really -- so it lives in ``core.config`` and arrives as an argument. The
domain does not read configuration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from persona_api.domain.errors import InvalidNoteError

if TYPE_CHECKING:
    from datetime import datetime

    from persona_api.domain.provenance import Source

NOTE_ID_PREFIX = "note_"


class NoteKind(StrEnum):
    """What sort of thing a note records.

    Three kinds, chosen because they are the three an assistant can tell apart while
    writing. A taxonomy with ten entries is one where nine are guesses.
    """

    EPISODE = "episode"
    """Something that happened, at a time -- the migration review that went badly."""

    OBSERVATION = "observation"
    """Something noticed about the world or the owner. Not necessarily datable."""

    LESSON = "lesson"
    """Something to do differently. The kind most worth surfacing unprompted."""


def new_note_id() -> str:
    """Return a fresh note id.

    Notes are identified by an id rather than by anything about their content, because
    two notes saying the same thing at different times are two notes -- which is exactly
    the difference between a note and a field.
    """
    return f"{NOTE_ID_PREFIX}{uuid.uuid4().hex}"


def normalize_body(raw: str, *, limit: int) -> str:
    """Trim a note body and refuse one that cannot be stored.

    Unlike card text, a blank body is an error rather than an absence: a note with
    nothing in it is a row that will be read, ranked and rendered forever without ever
    saying anything.

    Args:
        raw: The body as the caller wrote it.
        limit: The maximum length in characters, from configuration.

    Returns:
        The trimmed body.

    Raises:
        InvalidNoteError: if the body is blank or longer than ``limit``.
    """
    body = raw.strip()
    if not body:
        msg = "a note body cannot be empty"
        raise InvalidNoteError(msg)
    if len(body) > limit:
        msg = f"a note body is at most {limit} characters, and this one is {len(body)}"
        raise InvalidNoteError(msg)
    return body


@dataclass(frozen=True, slots=True)
class Note:
    """One free-text memory.

    ``forgotten_at`` is a tombstone rather than a deletion. Forgetting is reversible
    here -- a note stops appearing in reads and comes back with
    ``include_forgotten=true`` -- because an assistant deciding on its own that a memory
    is stale should not be able to destroy it.

    ``revision`` counts edits to the body. It exists so that a caller holding a stale
    copy can tell, and so an event can say "revised" without carrying the text.
    """

    note_id: str
    account_id: str
    profile: str
    body: str
    kind: NoteKind
    source: Source
    asserted_by: str
    pinned: bool
    revision: int
    created_at: datetime
    updated_at: datetime
    forgotten_at: datetime | None
