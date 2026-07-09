"""Tests for :mod:`app.utils`."""

from __future__ import annotations

import pytest

from app.utils import bytes_to_human, format_duration, truncate_text
from app.utils.formatters import join_nonempty


class TestBytesToHuman:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.00 KiB"),
            (1024 * 1024, "1.00 MiB"),
            (int(1024**3 * 4.5), "4.50 GiB"),
        ],
    )
    def test_positive(self, value: int, expected: str) -> None:
        assert bytes_to_human(value) == expected

    def test_negative(self) -> None:
        assert bytes_to_human(-2048).startswith("-2.00")


class TestFormatDuration:
    def test_milliseconds(self) -> None:
        assert format_duration(0.05).endswith("ms")

    def test_seconds(self) -> None:
        assert format_duration(1.5) == "1.50 s"

    def test_minutes(self) -> None:
        assert format_duration(75) == "1m 15.00s"

    def test_hours(self) -> None:
        assert format_duration(3725).startswith("1h ")

    def test_days(self) -> None:
        assert format_duration(3600 * 25).startswith("1d ")


class TestTruncateText:
    def test_short_text_passes_through(self) -> None:
        assert truncate_text("hello", max_chars=100) == "hello"

    def test_long_text_is_truncated(self) -> None:
        result = truncate_text("x" * 500, max_chars=100)
        assert "[truncated" in result
        assert len(result) <= 100

    def test_head_mode(self) -> None:
        result = truncate_text("x" * 500 + "END", max_chars=100, tail=False)
        assert result.startswith("x")
        assert "[truncated" in result

    def test_too_small_max_chars(self) -> None:
        with pytest.raises(ValueError):
            truncate_text("x", max_chars=10)


def test_join_nonempty() -> None:
    assert join_nonempty(["a", "", "b", None, "c"]) == "a\nb\nc"  # type: ignore[list-item]
