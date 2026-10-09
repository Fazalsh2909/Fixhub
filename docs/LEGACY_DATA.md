# Legacy ownerless data policy (Phase 4.5)

Some rows predate FixHub authentication and have `owner_id = NULL`:

- `repositories.owner_id`
- `tasks.owner_id`
- `memories.owner_id`

## Policy: inaccessible forever

- `NULL` never matches any user. All ownership queries filter
  `owner_id == <authenticated user id>`, which is never true for `NULL`.
- These rows are kept only for historical integrity (foreign-key targets,
  event-trail continuity). They are invisible through every normal API:
  listing, detail, files, terminal, events, memories, PR views.
- Do NOT assign them to a real user, silently or otherwise. Genuine
  user-owned legacy data requires explicit manual reassignment by an
  operator who verifies ownership out of band.
- Webhooks never adopt legacy rows into new tasks: ownership resolves from
  the trusted user→installation mapping, and unconnected installations are
  ignored without creating tasks.

## Tests

`backend/tests/test_phase45.py::test_legacy_null_rows_invisible` and
`test_legacy_null_never_adopted_by_webhook` enforce this permanently.
