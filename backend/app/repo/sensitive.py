"""Centralized sensitive-file access policy (P0-3).

One rule set, enforced consistently by every surface that touches file
CONTENTS: agent read/edit/create tools, repository browsing reads, the code
indexer, search, and shell commands (cat/ls/git show). File NAMES may still
appear in directory listings — names are needed for engineering; contents
are never disclosed.

A blocked read returns ``Access denied: sensitive file.`` — never the
contents, never a hint about what is inside.
"""

from __future__ import annotations

DENIED_MESSAGE = "Access denied: sensitive file."

# Exact basenames that are secrets by definition.
SENSITIVE_BASENAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "token.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "api_key",
        "api_key.txt",
        "token.txt",
    }
)

# Basename prefixes: `.env.<anything>`, key material, secret/token files.
SENSITIVE_PREFIXES = (".env.", "id_rsa", "id_ed25519", "id_ecdsa", "token.", "secret.")

# Basename suffixes: key stores and credential blobs.
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", "-credentials.json")

# Directory components that are credential stores by definition.
SENSITIVE_DIRS = frozenset(
    {".git", ".aws", ".ssh", ".gnupg", ".azure", ".kube", ".docker"}
)


def is_sensitive(rel: str) -> bool:
    """True when a workdir-relative posix path must not have its contents
    exposed. Case-sensitive (secrets tooling is case-sensitive too)."""
    if not rel:
        return False
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(p in SENSITIVE_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in SENSITIVE_BASENAMES:
        return True
    if name.startswith(SENSITIVE_PREFIXES):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return False
