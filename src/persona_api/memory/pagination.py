"""One page of rows, and the cursor that follows it.

Keyset pagination, not offset. The cursor encodes the last ``(sort_key, row_id)`` a
caller saw, so the next page is defined by the row it follows rather than by how many
rows came before it -- which is what makes "retrieve everything" correct on a persona
somebody is still writing to. An offset shifts when a row is inserted above it, and a
walk across a concurrent write then shows one row twice and skips another.

Every list here asks the database for **one row more than it will return**. The presence
of that extra row is how the page knows there is another one, without a second ``count``
query that could disagree with the first -- and without the off-by-one where a full
final page hands back a cursor for an empty one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """Rows, and where to carry on from."""

    items: list[T]
    next_cursor: str | None
    """``None`` when this is the last page. Never a cursor for an empty page."""
