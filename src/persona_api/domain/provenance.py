"""Where a claim came from.

Every field and every note carries two attributions, and they are not the same kind of
thing at all:

``source`` is a **claim**. The caller says whether the owner told the assistant this,
whether the assistant concluded it, or whether some other service supplied it. Nothing
verifies it, and nothing could -- an assistant writing "the owner told me" is a
statement about a conversation this service never saw.

``asserted_by`` is **server-derived**. It is the ``aud`` of the verified token, so it
says which audience the token was minted for and cannot be forged by a request body.

Keeping both, and being honest in the schema about which is which, is what lets a reader
of a persona weigh what it says -- see ``docs/adr/0004-provenance-is-partly-a-claim.md``.
It is also the mechanism behind the invariant that a persona is data rather than
instructions: a note rendered as "the assistant recorded, via persona-mcp, that ..." is
a reported claim, and a reported claim is much harder to mistake for an order.
"""

from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    """Who is said to have asserted a field or a note.

    A closed set rather than free text, for the same reason ``NoteKind`` is closed: a
    typo in a free-string source is a memory that can never be filtered for again, and
    nobody notices until they go looking for everything the owner said.
    """

    OWNER = "owner"
    """The person whose account this is said it."""

    ASSISTANT = "assistant"
    """The assistant concluded it. The default, and the one to be most sceptical of."""

    SERVICE = "service"
    """Another system supplied it -- an import, a sync, a scheduled job."""
