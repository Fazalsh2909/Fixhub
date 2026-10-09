"""Phase 4 migration runner: `alembic upgrade head` for production.

Production rule (Gate 0): a failed migration MUST stop startup. There is no
silent `create_all()` continuation and no partial schema initialization in
prod — a half-migrated database serving traffic is worse than a refused boot.

Dev/test keep `create_all` bootstrapping as an explicit, loud fallback.
"""

from __future__ import annotations


class MigrationFailed(RuntimeError):
    """Alembic could not bring the database to head (fail-closed signal)."""


def _is_prod() -> bool:
    try:
        from app.config import settings as _settings

        return str(getattr(_settings, "ENV", "dev") or "dev").strip().lower() == "prod"
    except Exception:
        return False


def upgrade_head(*, strict: bool | None = None, revision: str = "head") -> bool:
    """Run migrations to `revision` (default head). Returns True on success.

    - strict=True (or None in production): raise MigrationFailed on ANY
      failure. Callers must let this stop startup.
    - strict=False, non-prod: return False on failure so dev/test can fall
      back to create_all bootstrapping (loudly, at the call site).
    """
    if strict is None:
        strict = _is_prod()
    try:
        from alembic import command as _command
        from alembic.config import Config as _Config

        import os as _os

        backend = _os.path.dirname(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        )
        cfg = _Config(_os.path.join(backend, "alembic.ini"))
        cfg.set_main_option("script_location", _os.path.join(backend, "alembic"))
        _command.upgrade(cfg, revision)
        return True
    except Exception as exc:
        if strict:
            raise MigrationFailed(f"alembic upgrade {revision} failed: {exc}")
        return False
