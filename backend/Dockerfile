FROM python:3.12-slim
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Validation toolchain, preinstalled so agents never burn runtime budget on
# environment setup. Repo-pinned versions still win (installed on top by the
# agent/gates when CI parity demands it); these are the instant fallback.
RUN pip install --no-cache-dir ruff mypy pytest
COPY . ./backend/
WORKDIR /srv/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
