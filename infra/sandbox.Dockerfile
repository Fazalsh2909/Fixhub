# Fixhub sandbox image: toolchain baked in so per-task runs are seconds, not minutes.
# Prod builds per-repo images; scaffold bakes the demo + common test deps.
# Per-repo runtime deps are installed by the verification pipeline's install
# gate (pip install -r requirements.txt) — the sandbox has PyPI egress.
# git is included: the coding agent inspects history/diffs (read-only).
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists \
    && pip install --no-cache-dir fastapi==0.115.6 httpx==0.28.1 PyJWT==2.10.1 pytest==8.3.4 pytest-asyncio==0.25.0 pytest-cov==6.0.0 ruff==0.9.2 mypy==1.14.1
WORKDIR /work
