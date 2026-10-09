# BYOK master-key rotation procedure (Phase 4.5)

User LLM credentials are encrypted with Fernet under
`FIXHUB_CREDENTIAL_ENCRYPTION_KEY`. Rotation re-encrypts every row with the
new key without ever exposing plaintext to operators or logs.

## Procedure

1. Generate the new key (never commit it):
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Set the new key as `FIXHUB_CREDENTIAL_ENCRYPTION_KEY` and move the current
   key to `FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` on every API/worker
   instance, then restart them.
   - During this window both keys decrypt (mixed state stays readable);
     only the current key encrypts.
3. As an admin, call `POST /api/llm/credentials/rotate`.
   - It re-encrypts every credential in ONE transaction, verifying each row
     decrypts under the new key first. Any failure rolls everything back.
   - Response: `{rotated, already_current, total, by}` — counts only, no secrets.
4. Verify: test a credential per provider (`POST /api/llm/credentials/test`)
   and confirm runtime tasks succeed.
5. Remove `FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` from the environment and
   restart. Keep the old key in the secrets manager (offline) until the next
   rotation, then destroy it.

## Safety properties

- The old key is never deleted by the tool, only superseded.
- Rollback on mid-batch failure leaves all rows under old encryption,
  still readable via the previous-key fallback.
- The endpoint is ADMIN-only (+CSRF); rotation runs are attributed (`by`).

## Tests

`backend/tests/test_phase45.py::test_rotation_migrates_old_key`,
`test_rotation_endpoint_admin_only`, `test_rotation_rollback_on_failure`.
