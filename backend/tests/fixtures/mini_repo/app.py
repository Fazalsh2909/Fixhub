"""Deterministic fixture: calc.add has a known bug (subtracts instead of adding)."""


def add(a, b):
    return a - b  # BUG: should add
