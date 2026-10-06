## HTTP and WebSocket contracts

Frozen surface for the FastAPI migration (Phase 0). Clients (NUnit plugin, collector, UI) depend on these paths and behaviors. Do not change them without updating clients and tests.

### Pages (HTML)

| Method | Path |
|--------|------|
| GET | `/` |
| GET | `/health` |
| GET | `/targets`, `/collections` |
| GET | `/analyzer`, `/matrix`, `/failures`, `/settings`, `/logs` |
| GET | `/login`, `/logout`, `/users` (present when `auth.enabled` is true) |
| GET | `/auth/oidc/login`, `/auth/oidc/callback` (OIDC; present when auth is on) |
| GET | `/targets/{key}`, `/targets/{key}/kpis`, `/collections/{key}` |
| GET | `/targets/{key}/{tool}` where tool is `analyzer`\|`matrix`\|`failures` |
| GET | `/collections/{key}/{tool}` (same tools) |
| GET | `/testRun/{run_id}/index.html` |
| GET | `/testRun/{run_id}/log/{test_case_id}.html` |
| GET | `/testRun/{tail}` (run static files) |
| GET | `/static/{path}` |
| GET | `/export/{run_id}.zip` |

### JSON / attachment APIs

| Method | Path |
|--------|------|
| GET | `/api/server-info` |
| GET | `/ca.crt` (generated auto CA only; 404 otherwise) |
| POST | `/api/admin/shutdown` |
| GET/POST | `/api/targets`, `/api/collections` |
| GET/PUT/DELETE | `/api/targets/{key}`, `/api/collections/{key}` |
| PUT | `/api/targets/{key}/complete-setup` |
| PUT | `/api/collections/{key}/members` |
| GET | `/api/collections/{key}/profile-filter-options` |
| POST | `/api/collections/{key}/profiles` |
| GET | `/api/collections/{key}/summary` |
| POST | `/api/collections/{key}/reports` |
| GET | `/api/run-set` |
| GET/PUT/DELETE | `/api/profiles/{profile_id}` |
| GET | `/api/test-runs`, `/api/test-runs/{run_id}` |
| GET/POST | `/api/runs/{run_id}/comments` |
| GET | `/api/runs/{run_id}/comments/log/{tc_id}` |
| PATCH/DELETE | `/api/comments/{comment_id}` |
| GET | `/api/comments/presence` |
| GET | `/api/test-results/for-runs`, `/api/test-results/over-time` |
| GET | `/api/test-case/history`, `/api/test-case/history-with-links` |
| GET | `/api/metadata/keys`, `/api/metadata/values` |
| GET | `/api/failures/toplist` |
| GET | `/api/classifications/{run_id}` |
| GET | `/api/tc-hover-history`, `/api/run-hover-history` |
| POST | `/api/migrate-data` |
| POST/GET | `/api/runs/{run_id}/commits` |
| POST/GET | `/api/runs/{run_id}/kpis` |
| GET | `/api/kpis/catalog`, `/api/kpis/dimension-options`, `/api/kpis/metrics`, `/api/kpis/runs`, `/api/kpis/series`, `/api/kpis/source-options`, `/api/kpis/history` |
| GET | `/api/runs/{run_id}/commit-baselines` |
| POST/GET | `/api/runs/{run_id}/analyze`, `/api/runs/{run_id}/analysis` |
| GET | `/api/runs/{run_id}/analysis/summary` |
| GET | `/api/runs/{run_id}/analysis/{tc_full_name}` |
| POST/GET | `/api/runs/{run_id}/analyze/{tc_full_name}/deep` |
| GET/PUT/DELETE | `/api/settings/email-recipients` |
| GET | `/api/settings/ai-usage` |
| GET | `/api/logs` |
| POST | `/api/auth/login`, `/api/auth/logout` |
| GET | `/api/auth/me` |
| GET/POST | `/api/users` |
| GET/PUT | `/api/users/{user_id}` |
| POST | `/api/users/{user_id}/reset-password`, `/api/users/{user_id}/unlock` |
| POST | `/api/attachments/{run_id}/{test_case_id}/upload` (if enabled) |
| GET | `/api/attachments/{run_id}/{test_case_id}/list` |
| GET | `/api/attachments/{run_id}/{test_case_id}/download/{filename}` |

### WebSockets (MessagePack binary frames)

| Path | Role |
|------|------|
| `/ws/nunit` | Test client ingestion |
| `/ws/ui` | UI live updates |
| `/ws/logs/{run_id}/{test_case_id}` | Live per-case log stream |

Message types and fields: see [websocket_protocol.md](websocket_protocol.md).

### Compatibility notes

- Authentication is off by default (`auth.enabled: false`): same open model as before; `localhost_only` still applies at bind time.
- When `auth.enabled` is true, unauthenticated HTML requests redirect to `/login` and unauthenticated JSON APIs return 401. Admin pages and Admin APIs require `admin.access`. `/health` and `/api/server-info` stay public. OIDC callback and start URLs are public so the identity provider can redirect back.
- When `auth.ingest_token` is set, `/ws/nunit` and test-client `POST` attachment/commit/KPI uploads require `X-TestRift-Ingest-Token` (or `Authorization: Bearer`). When it is empty, those ingest paths stay open.
- KPI upload accepts a UTF-8 JSON object with `_schemaVersion: 1` and an `_samples` array (maximum 10 MiB and 10,000 samples). Uploads are run-bound; identical payload retries are idempotent, and a corrected payload atomically replaces that source's prior batch. The legacy category arrays may remain alongside `_samples`.
- KPI reads support `target`, `run_id`, `metric_key`, `unit`, ISO-8601 `from`/`to`, typed `dimension.<name>` filters, and `limit`/`offset`. Repeat `target` to query up to 50 targets together (for example, `?target=device-a&target=device-b`); target filters are inclusive, and KPI sample/history rows retain their `target_key`. Series requests require both `metric_key` and `unit`; different keys and units are never implicitly combined.
- When `auth.ingest_token` is set, the KPI `POST` endpoint also requires `X-TestRift-Ingest-Token` (or `Authorization: Bearer`). When `tls.ingest` is on, KPI uploads must use HTTPS on the ingest listener.
- When `tls.ingest` is on, those ingest paths must use HTTPS on the ingest listener. HTTP to them returns 400. `GET /api/server-info` includes `ingest_url` and `tls_ca_fingerprint`. See [tls.md](tls.md).
- Health returns `{"status": "ok"}`.
- Most JSON APIs use `{"success": true|false, ...}` envelopes.
- Wire protocol for WebSockets must remain MessagePack with existing field shortcuts.
