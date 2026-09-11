"""What a caller may narrow a listing by.

Every filter here is backed by an index -- see the comment above the partial indexes in
``0001_initial.sql`` and the ``EXPLAIN QUERY PLAN`` assertions in
``tests/unit/storage/test_schema.py``. A filter that could not use an index is not a
filter this service offers, because the alternative is an endpoint that is fast on the
persona it was tested against and a scan on the one that matters.

One asymmetry is deliberate and is the only place the two shapes differ:

* **Notes are ordered and range-filtered on ``created_at``.** A note is something that
  happened, and when it happened is what you ask about.
* **Fields are ordered and range-filtered on ``updated_at``.** A field is current state,
  and when it last changed is what you ask about.

Each list endpoint's ``since``/``until`` therefore names the column its own index is
built on. Documented in ``docs/api.md`` as well, because a caller cannot see an index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from persona_api.domain.notes import NoteKind
    from persona_api.domain.provenance import Source


@dataclass(frozen=True, slots=True)
class FieldFilters:
    """How to narrow a listing of fields."""

    query: str | None = None
    """Full text, over the rendered projection: key, description and flattened value."""

    source: Source | None = None
    pinned: bool | None = None
    """``None`` means "either". ``False`` means "only the unpinned ones"."""

    key_prefix: str | None = None
    """``forms_`` finds every key under it, served by the fields primary key."""

    keys: tuple[str, ...] = ()
    """Fetch several named fields at once, so an assistant makes one call not thirty."""

    since: datetime | None = None
    until: datetime | None = None
    """Inclusive lower and exclusive upper bound on ``updated_at``."""

    include_forgotten: bool = False
    """Bring tombstoned rows back into the listing. Off by default, which is the point."""


@dataclass(frozen=True, slots=True)
class NoteFilters:
    """How to narrow a listing of notes."""

    query: str | None = None
    kind: NoteKind | None = None
    source: Source | None = None
    pinned: bool | None = None

    since: datetime | None = None
    until: datetime | None = None
    """Inclusive lower and exclusive upper bound on ``created_at``. See the module docstring."""

    include_forgotten: bool = False
