"""Demo calculator: total() has a real off-by-one bug (returns a + b + 1)."""


def total(a: int, b: int) -> int:
    return a + b + 1


def format_total(a: int, b: int) -> str:
    return f"{a}+{b}={total(a, b)}"
