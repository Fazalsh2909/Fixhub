"""Phase 5 sandbox package: backend-dispatched execution.

`app.sandbox.sandbox` keeps the legacy host implementation + shared
redaction/policy helpers. `app.sandbox.backend` dispatches run_command to the
configured backend (host | firecracker) with fail-closed semantics.
"""

from app.sandbox.backend import (  # noqa: F401
    ExecResult,
    SandboxUnavailable,
    active_backend_name,
    get_backend,
)
from app.sandbox.sandbox import (  # noqa: F401
    CommandResult,
    HostBackend,
    SandboxBlockedError,
    redact,
    register_secret,
)
