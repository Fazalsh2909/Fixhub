# Issue: total() returns a sum that is off by one

`calc.total(2, 3)` returns `6` instead of `5`.

Repro: `python -m pytest tests/test_total.py -q` → FAIL (`assert 6 == 5`).

Expected: `total(a, b) == a + b`.
