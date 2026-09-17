# Bundled agent skills (vendored)

Curated subset of the ECC/opencode skill pack, shipped inside the backend
image so deployed agents get expert workflows with zero host setup.

## Contents

- `tdd-workflow` — test-driven fix loop (also the selection fallback)
- `verification-loop` — verify work before claiming done
- `security-review` — auth/input/secrets hardening checklist
- `ai-regression-testing` — regression coverage for AI-written code
- `error-handling` — robust error design (TS/Python/Go)
- `e2e-testing` — Playwright patterns for UI paths
- `click-path-audit` — trace UI actions through state changes

## Origin

Vendored from the locally installed opencode skill pack
(`~/.config/opencode/skills/`, `origin: ECC`). Guidance text only — the
sandbox + policy gate remain the enforcement boundary.

## Updating

Re-copy from a newer pack, keeping the `<name>/SKILL.md` layout:

```powershell
foreach ($s in @('tdd-workflow','verification-loop','security-review','ai-regression-testing','error-handling','e2e-testing','click-path-audit')) {
  Copy-Item "$env:USERPROFILE\.config\opencode\skills\$s\SKILL.md" "backend\app\agent\skills_bundle\$s\SKILL.md" -Force
}
```

To use the full local pack instead in dev, set `SKILLS_DIR=~/.config/opencode/skills`.
To disable: `SKILLS_ENABLED=false`.
