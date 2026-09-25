"""Regression test for the total() off-by-one bug. FAILS before the fix."""

from calc import total


def test_total():
    assert total(2, 3) == 5
