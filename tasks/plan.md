# Implementation Plan: Fixhub broad scaffold

## Overview
Thin-but-real slice of all 18 phases so demo + webhook→PR path works end-to-end. No fakes: every green check maps to real execution.

> Status: scaffold implemented (see `backend/app/`, `frontend/src/`, `infra/`); verification gates (`pytest`, `tsc`, `vite build`, compose config) run before marking phases done.

## Task List
### Phase 0-1: Foundation
- [x] Spec + architecture + ADRs
- [ ] Backend foundation (config, db, logging, queue, health) + pytest green
- [ ] Frontend shell boots (Vite build green)
### Phase 7,2: LLM + GitHub
- [ ] OpenRouterProvider live call (1 tool_call) behind env key
- [ ] Webhook verify + idempotency + read client + publisher guard
### Phase 3-5: Workspace/Intel/Memory
- [ ] Clone + git ops + file/symbol index + memory CRUD/freshness/retrieval
### Phase 6,8-12: Loop
- [ ] Allow-listed tools + orchestrator state machine + Docker sandbox run + verification + proof + policy gate
### Phase 14-16: UI/Demo/Eval
- [ ] IDE layout + Monaco + proof panel; demo JWT repo FAIL→PASS; eval seeds + metrics
### Phase 17-18: Ship
- [ ] Compose + terraform stub + hardening + Playwright green

## Risks
| Risk | Impact | Mitigation |
| free-model rate limits | Med | budgets, retries, cached intel |
| Docker on Windows | Med | fallback local-temp workspace with same interface |
| GitHub App tunnel | Low | smee/ngrok docs; demo needs no tunnel |
