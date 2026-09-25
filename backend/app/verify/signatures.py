"""Normalized failure signatures for baseline-vs-after attribution.

Raw stdout is unstable across runs (line shifts, timings, memory
addresses, absolute paths). Signatures reduce a gate output to a sorted
set of stable identities — `pytest:path::test:ErrorType`,
`mypy:path:code`, `pytest-collect:path` — so "same failure before and
after the patch" is a set comparison, not string matching.

Process/environment markers (missing executable, missing module,
connection refused, timeout, policy denial) are detected separately:
they classify the failure without needing a baseline.
"""

from __future__ import annotations

import re

_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_TIMING = re.compile(r"in \d+(?:\.\d+)?s\b")
_WS = re.compile(r"\s+")

_PYTEST_FAIL = re.compile(r"^FAILED\s+(\S+?)\s*-\s*([A-Za-z_][\w.]*)", re.MULTILINE)
_PYTEST_COLLECT = re.compile(r"^ERROR\s+(?:collecting\s+)?(\S+\.py)\b", re.MULTILINE)
_MYPY = re.compile(
    r"^(.*?\.py):\d+:\s*(?:error|note):\s*(.*?)(?:\[([a-z-]+)\])?\s*$",
    re.MULTILINE,
)
_EXEC_NOT_FOUND = re.compile(r'exec:\s*"([^"]+)"')
_NO_MODULE = re.compile(r"No module named ['\"]?([\w.]+)['\"]?")


def _norm_msg(text: str) -> str:
    """Strip unstable tokens (addresses, timings) and squash whitespace."""
    text = _HEX_ADDR.sub("0x…", text)
    text = _TIMING.sub("in …s", text)
    return _WS.sub(" ", text).strip()


def _norm_path(path: str) -> str:
    """Repo-relative, forward slashes, no ./ or /work/ prefix."""
    path = path.replace("\\", "/").strip()
    path = re.sub(r"^(\./|/work/)+", "", path)
    path = re.sub(r"^[A-Za-z]:/", "", path)
    return path


def parse_pytest(output: str) -> list[str]:
    """Extract `pytest:<node>:<ErrorType>` + `pytest-collect:<path>` signatures."""
    sigs: list[str] = []
    for node, err in _PYTEST_FAIL.findall(output or ""):
        sigs.append(f"pytest:{_norm_path(node)}:{err.split('.')[-1]}")
    for path in _PYTEST_COLLECT.findall(output or ""):
        sig = f"pytest-collect:{_norm_path(path)}"
        if sig not in sigs:
            sigs.append(sig)
    return sorted(sigs)


def parse_mypy(output: str) -> list[str]:
    """Extract `mypy:<path>:<code>` signatures (line numbers ignored)."""
    sigs: list[str] = []
    for path, _msg, code in _MYPY.findall(output or ""):
        sig = f"mypy:{_norm_path(path)}:{code or 'error'}"
        if sig not in sigs:
            sigs.append(sig)
    return sorted(sigs)


def parse_process_markers(output: str) -> tuple[str | None, str]:
    """Detect environment/dependency/timeout/config failures.

    Returns (category, marker) where category is one of ENVIRONMENT,
    DEPENDENCY, TIMEOUT, CONFIGURATION — or (None, "") when the output
    looks like a genuine test/type result.
    """
    text = output or ""
    m = _EXEC_NOT_FOUND.search(text)
    if m:
        return ("ENVIRONMENT", f"missing-executable:{m.group(1)}")
    if text.strip() == "timeout":
        return ("TIMEOUT", "timeout")
    m = _NO_MODULE.search(text)
    if m:
        return ("DEPENDENCY", f"missing-module:{m.group(1).split('.')[0]}")
    if re.search(r"[Cc]onnection refused", text):
        return ("ENVIRONMENT", "connection-refused")
    if "denied by tool policy" in text:
        return ("CONFIGURATION", "policy-denial")
    # mypy missing stubs / py.typed marker: env has the package but not its
    # types — a dependency/environment issue, never the agent's patch.
    if "missing library stubs" in text or "py.typed marker" in text:
        return ("DEPENDENCY", "missing-type-stubs")
    if "Skipping analyzing" in text:
        return ("DEPENDENCY", "skipped-analyzing")
    return (None, "")


def gate_signatures(check: str, output: str) -> list[str]:
    """All normalized signatures for one gate output."""
    if check in ("suite", "regression", "repro", "impacted"):
        return parse_pytest(output)
    if check == "type":
        return parse_mypy(output)
    return []


def compare_signatures(baseline: list[str], after: list[str]) -> dict[str, list[str]]:
    """Set-compare failure identities. Returns sorted new/resolved/unchanged."""
    base, aft = set(baseline or []), set(after or [])
    return {
        "new": sorted(aft - base),
        "resolved": sorted(base - aft),
        "unchanged": sorted(base & aft),
    }
