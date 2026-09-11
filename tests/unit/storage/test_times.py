"""How a moment becomes a column, and the two properties that fail silently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from persona_api.storage.times import (
    from_column,
    from_column_optional,
    to_column,
    to_column_optional,
)


class TestTimezoneSurvivesTheRoundTrip:
    def test_a_naive_datetime_is_refused_rather_than_guessed_at(self) -> None:
        # A naive datetime here means a caller read the wall clock directly instead of
        # taking the injected one. Guessing a zone would turn that bug into data.
        with pytest.raises(ValueError, match="naive"):
            to_column(datetime(2026, 3, 1, 12, 0))  # noqa: DTZ001

    def test_it_comes_back_aware(self) -> None:
        # Nothing in this service compares a naive datetime, so a column that returned
        # one would raise at the first comparison -- far from the store that wrote it.
        assert from_column(to_column(datetime(2026, 3, 1, tzinfo=UTC))).tzinfo is not None

    def test_a_non_utc_datetime_is_normalized_rather_than_rejected(self) -> None:
        lisbon = timezone(timedelta(hours=1))
        noon_in_lisbon = datetime(2026, 3, 1, 12, 0, tzinfo=lisbon)

        assert from_column(to_column(noon_in_lisbon)) == noon_in_lisbon

    def test_the_instant_is_preserved_exactly(self) -> None:
        moment = datetime(2026, 3, 1, 12, 34, 56, 789_012, tzinfo=UTC)

        assert from_column(to_column(moment)) == moment


class TestOrderingMatchesChronology:
    def test_lexicographic_order_is_chronological_order(self) -> None:
        # Every cursor page, every `since` filter and every "newest first" listing
        # sorts on these columns, in SQL, as text. That only works if the format is
        # fixed width.
        moments = [
            datetime(2026, 3, 1, 12, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 1, 12, 0, 0, 1, tzinfo=UTC),
            datetime(2026, 3, 1, 12, 0, 1, 0, tzinfo=UTC),
            datetime(2026, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
            datetime(2027, 1, 1, 0, 0, 0, 0, tzinfo=UTC),
        ]
        rendered = [to_column(moment) for moment in moments]

        assert rendered == sorted(rendered)

    def test_a_zero_microsecond_stamp_is_the_same_width_as_any_other(self) -> None:
        # isoformat() drops the fractional part when it is zero. A stamp that did that
        # would sort AFTER one that kept it, and the oldest row would sometimes not be
        # the one that went -- silently, and only for rows written on a whole second.
        on_the_second = to_column(datetime(2026, 3, 1, 12, 0, 0, 0, tzinfo=UTC))
        just_after = to_column(datetime(2026, 3, 1, 12, 0, 0, 1, tzinfo=UTC))

        assert len(on_the_second) == len(just_after)
        assert on_the_second < just_after


class TestOptionalStamps:
    def test_absent_stays_absent_in_both_directions(self) -> None:
        assert to_column_optional(None) is None
        assert from_column_optional(None) is None

    def test_a_present_stamp_round_trips(self) -> None:
        moment = datetime(2026, 3, 1, tzinfo=UTC)
        rendered = to_column_optional(moment)

        assert rendered is not None
        assert from_column_optional(rendered) == moment
