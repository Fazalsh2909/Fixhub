# Sandbox limitations (honest)
- Per-task Docker container is NOT a microVM: shared host kernel, so kernel exploits are out of scope.
- Egress allowlist is best-effort at this layer; true deny-by-default proxy is prod work.
- Allowed domains (pypi, npm, tokenrouter, github) can carry user-generated content — treat as exfil channel, review diffs before publish.
- Workspace is the trust boundary: review sandbox-modified files like an untrusted PR before running on host.
- Secrets never enter the container env; LLM keys stay host-side.
