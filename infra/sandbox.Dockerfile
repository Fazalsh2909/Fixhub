# Fixhub sandbox image: toolchain baked in so per-task runs are seconds, not minutes.
# Prod builds per-repo images; scaffold bakes the demo + common test deps.
FROM python:3.11-slim
RUN pip install --no-cache-dir fastapi==0.115.6 httpx==0.28.1 PyJWT==2.10.1 pytest==8.3.4 ruff==0.9.2
WORKDIR /work
