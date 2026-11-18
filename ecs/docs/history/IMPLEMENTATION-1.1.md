# 1.1 implementation contract

> 历史阶段记录：文中的旧目录、职责分工和当时的部署状态按记录日期保留；当前结构与操作入口以[根 README](../../../README.md)为准。

Approved scope: local development and isolated synthetic validation only. No production access or GitHub upload. Baseline: `baseline-1.0-20261116`.

## Boundaries

SSH credentials belong only to the local SSH launcher. The application must not read, mount, hash, or depend on ECS keys, addresses, usernames, or instance identities. Source is reusable; private state is reconstructable from a database dump and the original master password or recovery key. Machine query-unlock keys are newly generated on a replacement server.

## Ownership

- Database agent: core crypto/store/service/query_unlock, new migration module, support stores and core tests. Do not edit web/app.py, existing session test file, deployment files or backup scripts.
- Session agent: web/sessions.py, web/routes/session.py, web/security.py and session-specific tests. Do not change core or app.py.
- Deployment agent: client, console.ps1, Dockerfile, compose.yaml, .dockerignore, .gitignore, ops/security, ops/models, new ops/deploy files and client tests. Preserve the existing real local configuration under ignored .local before replacing tracked templates. Do not edit backup scripts or root documentation.
- Backup agent: new backup modules and routes, ops/backup and backup unit tests. Do not edit app.py, Dockerfile, compose.yaml or core files owned by the database agent.
- Frontend agent: all web static JS/CSS/templates and frontend tests.
- Acceptance agent: isolated integration harness and recovery guide.
- Main agent: app.py assembly, shared contracts, final documentation/version and integration fixes.

## Shared interfaces

- Keep `VaultService(store, envelope_path, query_key_path=None)` compatible where practical: envelope_path identifies the runtime directory only; normal envelope access is database-backed. `service.envelope.exists` remains available. Publish new Store/migration interfaces to main as soon as settled. Legacy envelope file is read only by explicit migration.
- Schema version 2: singleton metadata stores exact original encrypted envelope bytes, vault UUID and crypto format 1. New initialization is atomic. Missing envelope plus any business data fails closed. Unknown versions fail before mutation. Migration verifies all stored encrypted payloads with supplied credentials before committing; no re-encryption.
- Session: `POST /api/session/activity`, CSRF protected, requires existing valid session, returns remaining times. `/api/status` adds `session: {idle_remaining_seconds, absolute_remaining_seconds}` when authenticated, null otherwise. Idle 300 seconds, absolute 3600 seconds, monotonic injectable clock. No implicit touching on checks or API reads. Preserve status 423 for locked requests. Never call service.lock for management logout.
- Backup service: `BackupManager(service, ...)`, explicit start/close lifecycle. `register(app, service, admins, manager)` for routes. POST /api/backups returns task; GET /api/backups returns `{tasks, limits}`; GET /api/backups/{id} returns task. GET task suffixes `/download`, `/checksum`, `/manifest`. Task fields: id, status (generating/ready/failed/expired), phase, created_at, completed_at, expires_at, filename, size_bytes, sha256, error_code, error (safe user text). Absent fields may be null. Create returns same active task on duplicate click. GET requests never extend session.
- Defaults: one generating job; two downloads; generation timeout 1800 s; download grant 1800 s per admitted request, continues after session lock; retention 7 days; web storage quota 5 GiB including staging; reserve 1 GiB. No Range/resume in v1.1. Authenticated metadata and native no-store attachment responses; no tokens in URLs. Durable task manifests; incomplete tasks fail on restart. Cleanup startup/hourly, active downloads pinned.
- Backup format: PostgreSQL 17 custom dump, no tar for new backups. All application tables, sequences, encrypted envelope and version metadata are in the dump. Separate sha256 and JSON manifest are conveniences, not extra recovery keys. Never dump machine key or query-unlock.json.
- Web backup path `/backups/web`; shared maintenance lock `/backups/.backup.lock`, host path `${VAULT_STATE_DIR}/backups/.backup.lock`. Nonblocking maintenance admission avoids holding business mutex during dump. Backup scripts/migration/restore use this same lock. Runtime state `/data`, socket `/var/run/postgresql`, machine key `/run/secrets/query_unlock_key`. Host layout `${VAULT_STATE_DIR}/data/{postgres,pgsocket,vault}`, secrets, backups, models. Default VAULT_STATE_DIR=/var/lib/wechat-vault; explicit legacy root allowed.
- Configuration env names: VAULT_BACKUP_DIR=/backups/web, VAULT_MAINTENANCE_LOCK=/backups/.backup.lock, VAULT_BACKUP_TIMEOUT_SECONDS=1800, VAULT_BACKUP_RETENTION_DAYS=7, VAULT_BACKUP_QUOTA_BYTES=5368709120, VAULT_BACKUP_RESERVE_BYTES=1073741824, VAULT_DOWNLOAD_TIMEOUT_SECONDS=1800, VAULT_DOWNLOAD_CONCURRENCY=2.
- Docker image supplies pg_dump and pg_restore major 17, no Docker socket, nonroot app; VERSION is copied into image. State directory and PostgreSQL tool validation must be explicit.

## Browser behavior

Trusted click/input/scroll report activity, throttled; timers/polling/downloads never do. On hidden/pagehide/manual lock, immediately clear state, broadcast lock to same-cookie tabs and best-effort revoke with keepalive. Offline event clears immediately. Failed requests use two health probes (3 s timeout, 2 s gap), ordinary request connection timeout 5 s. Restore requires reauthentication; persisted pages must not restore credentials. Cancel in-flight requests and reject stale generations. No business plaintext in browser storage. Show download as started, not verified. Existing downloads continue for their admitted grant.

## Acceptance

Existing tests plus migration/failure/clock tests. Local Docker only, fresh databases and synthetic secrets. Restore from clean source plus one downloaded dump, new SSH identity, new machine key and original master/recovery; no old envelope/runtime files. Exercise old backup import, unknown schema, wrong credentials, interrupted jobs, disk quota, >512 MiB transfer with bounded memory, logout responsiveness and ciphertext inspection. Do not label release fully validated if real isolated recovery is unavailable.
