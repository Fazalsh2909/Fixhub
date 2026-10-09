"""Phase 4 migration runner: `alembic upgrade head` for production.

Dev/test keep create_all bootstrapping. This wrapper never raises: when
alembic is unavailable or the database is unreachable at import/startup time,
callers fall back to create_all and surface the real error there.
"""

from __future__ import annotations


def upgrade_head() -> bool:
    """Run migrations to head. Returns True on success, False otherwise."""
    try:
        from alembic import command as _command
        from alembic.config import Config as _Config

        import os as _os

        backend = _os.path.dirname(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        )
        cfg = _Config(_os.path.join(backend, "alembic.ini"))
        cfg.set_main_option("script_location", _os.path.join(backend, "alembic"))
        _command.upgrade(cfg, "head")
        return True
    except Exception:
        return False
