"""Opaque pagination cursors.

A cursor encodes the last ``(sort_key, row_id)`` a caller saw. That pair, rather than an
offset, is what makes "retrieve everything" correct on a persona somebody is still
writing to: an offset shifts when a row is inserted above it, so a walk across a
concurrent write shows one row twice and skips another. A keyset seek cannot -- the next
page is defined by the row it follows, not by how many rows came before it.

**Opaque on purpose.** It is base64 of two fields rather than a readable
``?after=2026-03-01``, because a caller that can read a cursor is a caller that will
construct one, and the shape of the pair is a storage decision that has to stay free to
change. Nothing is signed: a forged cursor can only ask for a page of the caller's own
data, which they could ask for anyway.

The separator is ``\\x1f``, the ASCII unit separator, so a sort key containing any
character a datetime or a field key can hold still round-trips. A comma would not.
"""

from __future__ import annotations

import base64
import binascii

from persona_api.domain.errors import InvalidCursorError

SEPARATOR = "\x1f"


def encode_cursor(sort_key: str, row_id: str) -> str:
    """Encode the last row of a page as the cursor that follows it."""
    packed = f"{sort_key}{SEPARATOR}{row_id}".encode()
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def decode_cursor(raw: str) -> tuple[str, str]:
    """Recover the ``(sort_key, row_id)`` a cursor was made from.

    Raises:
        InvalidCursorError: if the cursor is not one this service issued. Every way of
            being wrong -- bad base64, bad UTF-8, a missing separator -- is the same
            error, because a caller holding a cursor we did not mint has nothing to
            learn from which.
    """
    padded = raw + "=" * (-len(raw) % 4)
    try:
        unpacked = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        msg = "that cursor was not issued by this service"
        raise InvalidCursorError(msg) from exc

    sort_key, separator, row_id = unpacked.partition(SEPARATOR)
    if not separator:
        msg = "that cursor was not issued by this service"
        raise InvalidCursorError(msg)
    return sort_key, row_id
