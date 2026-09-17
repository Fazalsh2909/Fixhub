"""Hybrid repo indexer: files + symbols (stdlib ast for py, regex for ts/js). Incremental-ready."""

from __future__ import annotations

import ast
import re
from pathlib import Path

LANGS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".go": "go",
}
_FUNC_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)|^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=",
    re.M,
)
_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)", re.M)


def index_repo(root: Path, commit_sha: str = "") -> tuple[list[dict], list[dict]]:
    files, symbols = [], []
    for p in root.rglob("*"):
        if (
            not p.is_file()
            or ".git" in p.parts
            or "node_modules" in p.parts
            or "__pycache__" in p.parts
        ):
            continue
        lang = LANGS.get(p.suffix, "")
        files.append(
            {
                "path": str(p.relative_to(root)),
                "language": lang,
                "commit_sha": commit_sha,
            }
        )
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if p.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        {
                            "name": node.name,
                            "kind": "function",
                            "file": str(p.relative_to(root)),
                            "line": node.lineno,
                        }
                    )
                elif isinstance(node, ast.ClassDef):
                    symbols.append(
                        {
                            "name": node.name,
                            "kind": "class",
                            "file": str(p.relative_to(root)),
                            "line": node.lineno,
                        }
                    )
        elif p.suffix in (".ts", ".tsx", ".js"):
            for m in _FUNC_RE.finditer(text):
                name = m.group(1) or m.group(2)
                symbols.append(
                    {
                        "name": name,
                        "kind": "function",
                        "file": str(p.relative_to(root)),
                        "line": text[: m.start()].count("\n") + 1,
                    }
                )
            for m in _CLASS_RE.finditer(text):
                symbols.append(
                    {
                        "name": m.group(1),
                        "kind": "class",
                        "file": str(p.relative_to(root)),
                        "line": text[: m.start()].count("\n") + 1,
                    }
                )
    return files, symbols


def search_code(root: Path, pattern: str, include: str = "*.py") -> list[dict]:
    import fnmatch

    out = []
    rx = re.compile(pattern)
    for p in root.rglob("*"):
        if not p.is_file() or not fnmatch.fnmatch(p.name, include):
            continue
        try:
            for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if rx.search(line):
                    out.append(
                        {
                            "file": str(p.relative_to(root)),
                            "line": i,
                            "text": line.strip()[:300],
                        }
                    )
                    if len(out) >= 200:
                        return out
        except OSError:
            continue
    return out
