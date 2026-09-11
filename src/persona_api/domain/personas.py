"""The identity card, and the profile name that addresses it.

One persona per ``(account_id, profile)``. The card itself is deliberately tiny -- a
name, pronouns, a sentence -- because everything else an assistant might want to
remember about itself is a field or a note, and those grow without a migration while
this does not.

**A profile name is validated for shape and nothing else.** persona-api cannot check
that a profile exists in keyring: there is no endpoint that would answer, and a signed
token carries no profile list. So ``"wrok"`` is not an error here, it is a new and empty
persona -- which is why ``list_personas`` exists and why the mistake is recoverable
rather than silent. See ``docs/adr/0005-one-persona-per-profile.md``.

**Card text is normalized, not merely checked.** An empty string and a string of spaces
both become ``None``, so "clear my pronouns" and "I never set pronouns" are one state in
the database instead of three that a reader has to know are the same.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_api.domain.errors import InvalidProfileError

if TYPE_CHECKING:
    from datetime import datetime

PERSONA_ID_PREFIX = "per_"

MAX_PROFILE_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 120
MAX_PRONOUNS_LENGTH = 40
MAX_SUMMARY_LENGTH = 600
"""A sentence or two. The summary is read on every turn, so its cost is a prompt cost.

The card exists to be small enough to always send. Anything that wants a paragraph
wants to be a note.
"""

_PROFILE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
"""Lowercase, no leading or trailing punctuation, never only dots.

The same expression keyring validates its profile names with, on purpose: a profile is
named once and then used in both services, and a name that one accepted and the other
refused would be discovered by a person halfway through setting up the second.
"""


def new_persona_id() -> str:
    """Return a fresh, opaque persona id.

    Opaque because it is stable and public: it appears in responses and will appear in
    MCP tool arguments. A readable id built out of the account and the profile would put
    both into every log line that mentions a persona.
    """
    return f"{PERSONA_ID_PREFIX}{uuid.uuid4().hex}"


def normalize_profile(raw: str) -> str:
    """Fold a caller's profile string to the one form this service stores.

    Lowercased and trimmed, so ``"Work"``, ``"work "`` and ``"work"`` address the same
    persona rather than making three of them. What survives that and still cannot be
    stored is refused rather than mangled further.

    Args:
        raw: The profile name as the caller wrote it.

    Returns:
        The normalized name.

    Raises:
        InvalidProfileError: if the name is empty, too long, or carries punctuation in a
            position that would make two visibly different names compare equal.
    """
    candidate = raw.strip().lower()
    if len(candidate) > MAX_PROFILE_LENGTH:
        msg = (
            f"a profile name is at most {MAX_PROFILE_LENGTH} characters, "
            f"and this one is {len(candidate)}"
        )
        raise InvalidProfileError(msg)
    if not _PROFILE.match(candidate):
        msg = (
            "a profile name must be lowercase letters, digits, '.', '-' or '_', "
            "and must start and end with a letter or a digit"
        )
        raise InvalidProfileError(msg)
    return candidate


def normalize_card_text(raw: str, *, what: str, limit: int) -> str | None:
    """Trim one card field, treating blank as absent.

    Args:
        raw: The text as the caller wrote it.
        what: The field's name, used in the error message. A caller sending a name, a
            pronoun set and a summary in one request needs to be told which of the three
            was too long.
        limit: The maximum length in characters.

    Returns:
        The trimmed text, or ``None`` if nothing but whitespace was given.

    Raises:
        InvalidProfileError: if the trimmed text is longer than ``limit``.
    """
    text = raw.strip()
    if not text:
        return None
    if len(text) > limit:
        msg = f"{what} is at most {limit} characters, and this one is {len(text)}"
        raise InvalidProfileError(msg)
    return text


@dataclass(frozen=True, slots=True)
class Persona:
    """One assistant's identity card, for one profile of one account.

    Frozen, like every other type here: a persona read out of the store and then mutated
    in place would be a change nobody recorded an event for.
    """

    persona_id: str
    account_id: str
    profile: str
    display_name: str | None
    pronouns: str | None
    summary: str | None
    created_at: datetime
    updated_at: datetime
