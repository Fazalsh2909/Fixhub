"""Unrelated healthy test: must stay green before and after the fix."""

from calc import format_total


def test_format_shape():
    out = format_total(1, 2)
    assert out.startswith("1+2=")
