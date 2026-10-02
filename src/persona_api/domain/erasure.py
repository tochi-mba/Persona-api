"""What forgetting one field or note does, as one person has chosen it.

Three answers, spelled exactly as user-api spells them, because a person who has decided
what "delete" means for one service should not have to answer a differently-shaped version
of the question for the next one:

* ``tombstone`` -- hide it and keep it, for ever. What persona-api did before anybody could
  choose, and what everybody still gets until they choose otherwise.
* ``grace`` -- hide it now and destroy it after ``grace_days``. Setting the key again inside
  the window revives a field, and the destruction is called off.
* ``immediate`` -- destroy it inside the request. No recovery.

## The decision is taken when something is forgotten, and written on the row

A forgotten row carries ``purge_after``: the instant it may be destroyed, or nothing. That
is the whole mechanism, and the two properties the catalogue asks for fall out of it.

**A change is never retroactive.** Switching to ``immediate`` does not destroy what is
already waiting out a grace period, and switching away from ``tombstone`` does not schedule
what is already tombstoned -- because neither row's ``purge_after`` is touched by a
settings change. Nothing re-reads the setting later and applies it to old rows.

**The sweeper needs nobody's token.** It destroys rows whose ``purge_after`` has passed,
which is a fact about the row, so it never has to ask settings-api what somebody chose.
user-api keeps its erasure settings in its own store for exactly that reason; persona-api
reads them from settings-api at the one moment a token is in hand, and keeps the answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

DEFAULT_GRACE_DAYS = 30
"""The catalogue's default, and user-api's: long enough to notice a mistake."""

MAX_GRACE_DAYS = 365
"""The catalogue's maximum. A larger value is settings-api's bug, and is not honoured."""


class ErasureMode(StrEnum):
    """What forgetting does. See the module docstring."""

    GRACE = "grace"
    IMMEDIATE = "immediate"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True, slots=True)
class ErasurePolicy:
    """One person's erasure choice, as it applies to the row being forgotten now."""

    mode: ErasureMode = ErasureMode.TOMBSTONE
    grace_days: int = DEFAULT_GRACE_DAYS
    """Only read when ``mode`` is ``grace``."""

    @property
    def destroys_now(self) -> bool:
        """Whether the row is destroyed inside the request that forgets it."""
        return self.mode is ErasureMode.IMMEDIATE

    def purge_after(self, forgotten_at: datetime) -> datetime | None:
        """When a row forgotten at ``forgotten_at`` may be destroyed, or ``None`` for never.

        ``None`` for ``immediate`` too: that row is gone before anything could read the
        answer, so there is nothing to schedule.
        """
        if self.mode is ErasureMode.GRACE:
            return forgotten_at + timedelta(days=self.grace_days)
        return None

    def describe(self, forgotten_at: datetime) -> str:
        """What the forget event's ``detail`` says will become of the row. Never a value.

        Empty for a tombstone, which is exactly what that event said before forgetting was
        a choice -- so nobody reading the log of somebody who never chose sees a change.
        """
        if self.destroys_now:
            return "erased"
        due = self.purge_after(forgotten_at)
        if due is not None:
            return f"erased after {due.isoformat()}"
        return ""


TOMBSTONE = ErasurePolicy()
"""What forgetting did before it was a choice, and what it does for anybody who has not made one."""
