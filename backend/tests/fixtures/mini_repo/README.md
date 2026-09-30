# mini_repo fixture (deterministic FixHub test target)

- Known bug: `app.add` subtracts instead of adding.
- Failing test: `tests/test_app.py::test_add`.
- CI: `.github/workflows/ci.yml` runs `python -m pytest tests/ -v`.
- Expected fix: `return a + b` in `app.py`.
