# PorsLabs Production Operations

## Data layout

- Primary database: Railway PostgreSQL through `DATABASE_URL`.
- Encrypted columns: `telegram_accounts.session_str` and
  `telegram_accounts.api_hash`.
- Uploads and SQLite migration archive: Railway volume mounted at `/data`.
- Revocable web logins: PostgreSQL table `device_sessions`.

Never remove `DATA_ENCRYPTION_KEY`. A database backup without its matching key
cannot restore Telegram sessions. Store an offline copy of the key in a
password manager controlled by PorsLabs.

## Backup and restore

Railway Point-in-Time Recovery archives WAL continuously, creates incremental
backups daily, and creates full backups weekly. Before a risky migration, also run:

```bash
railway postgres pitr backup create --service Postgres --name pre-migration
```

To restore without touching the live database, create a sibling database:

```bash
railway postgres pitr restore --service Postgres --at 30m
```

Verify row counts and encrypted Telegram columns on the restored service before
changing the app's `DATABASE_URL` reference. The original database remains live
until cutover.

## Encryption key rotation

1. Keep the current value temporarily as `DATA_ENCRYPTION_KEY_OLD`.
2. Generate and set a new `DATA_ENCRYPTION_KEY`.
3. Redeploy; every subsequent account write uses the new key. The migration
   helper can re-save existing rows to rotate their ciphertext.
4. Verify every Telegram account can be decrypted with only the new key.
5. Remove `DATA_ENCRYPTION_KEY_OLD`.

Never rotate by overwriting the current key without retaining the previous key
during the transition.

## Device sessions

Users manage sessions at `/security/sessions`. Revoking a row immediately makes
the corresponding signed browser cookie unusable. Password reset revokes all
active device sessions for that user.
