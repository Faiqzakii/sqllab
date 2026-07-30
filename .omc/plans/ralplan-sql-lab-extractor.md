# RALPLAN: SQL Lab Extractor

**Status: pending approval**  
**Mode: deliberate**  
**Scope: planning only; tidak ada source code yang diubah.**

## Requirements Summary

Bangun CLI Python minimal untuk menjalankan query SQL yang telah diotorisasi pada SQL Lab `fasih-dashboard.bps.go.id`, lalu mengambil hasil melalui API SQL Lab tanpa bergantung pada batas tampilan UI 1000 baris/25 kolom. Alur HAR `HTTPToolkit_2026-07-27_08-37.har` menunjukkan:

1. Keycloak OIDC auth-code flow pada `sso.bps.go.id`, realm `pegawai-bps`.
2. Callback aplikasi `GET /oidc_callback`, lalu session aplikasi.
3. Bootstrap SQL Lab `GET /superset/sqllab/` dan metadata database `GET /api/v1/database/`.
4. Submit `POST /api/v1/sqllab/execute/` dengan `database_id`, `schema`, `sql`, `queryLimit`, `runAsync`, dan field SQL Lab lain.
5. HAR also captured `GET /api/v1/query/updated_since` approximately every 2 seconds.
6. Fetch `GET /api/v1/sqllab/results/?q=(key:<resultsKey>,rows:<n>)`.

Bukti HAR: request memakai `queryLimit=100000`; response memiliki `limit=5000`, `limitingFactor=NOT_LIMITED`, 14 baris, 17 kolom. Workbook `sqllab_ub_nilai_tambah_20260724T152625.xlsx` memiliki 14 data row dan 17 kolom, tetapi tidak menampilkan kolom `assignment_id`; kolom `link_fasih` memuat 14 UUID assignment yang tampak berbeda. Ini membuktikan sample memiliki identifier assignment yang berbeda, bukan membuktikan uniqueness database secara global. Batas UI 1000/25 dan batas server >5000 belum terbukti. Tidak ada endpoint export yang tertangkap.

## RALPLAN-DR

### Principles

1. **Authorized access only** — gunakan session milik user; jangan bypass permission, hard limit server, WAF, MFA, atau policy data.
2. **Secret-minimal** — credential, OIDC code/state/nonce, cookie, CSRF token, dan data hasil tidak masuk source, CLI args, log, fixture, atau git.
3. **Capability before optimization** — ukur batas aktual API lebih dulu; partitioning hanya dipakai bila diperlukan dan dapat dibuktikan konsisten.
4. **Auditable completeness** — hasil besar harus streaming dan dapat diaudit; validasi struktur SQL, snapshot count, page coverage, schema, dan reconciliation key bila dideklarasikan. Determinisme semantik `ORDER BY` tetap tanggung jawab caller dan tidak dibuktikan script.
5. **Boring implementation** — Python stdlib terlebih dahulu; browser hanya untuk bootstrap SSO bila plain request tidak layak.

### Decision Drivers

1. Keamanan session SSO dan pencegahan kebocoran data.
2. Kelengkapan serta konsistensi hasil melewati batas UI/API.
3. Operasional sederhana: dapat dijalankan ulang, diamati, dan diverifikasi tanpa dependency berat.

### Viable Options

**A — Hybrid browser bootstrap + full API query state machine (dipilih)**
- Pros: cocok dengan OIDC/MFA; mendukung response synchronous dan async/non-terminal; API mengambil data efisien; session tetap ephemeral.
- Cons: state machine dan handoff cookie/CSRF menambah test matrix; browser bootstrap tetap sensitif terhadap perubahan login UI.

**B — Plain HTTP OIDC client end-to-end**
- Pros: tanpa browser; mudah dipasang sebagai job/headless CLI.
- Cons: rapuh terhadap MFA, CAPTCHA, dynamic Keycloak form/WAF; berisiko mendorong credential handling yang tidak aman. Hanya fallback eksperimen bila policy dan environment mengizinkan.

**C — Browser automation untuk seluruh query dan extraction**
- Pros: paling dekat dengan alur yang sudah terbukti di HAR.
- Cons: lambat, rapuh terhadap rendering/virtualization, sulit menjamin seluruh baris/kolom, dan tidak perlu untuk endpoint API yang sudah terlihat.

**Invalidation rationale:** B tidak dipilih sebagai default karena HAR menunjukkan login POST dinamis dan kemungkinan MFA/WAF; C tidak dipilih karena UI justru sumber batas yang ingin dihindari. Keduanya tetap fallback/diagnostic, bukan bypass.

## Architecture and Data Flow

### Planned files (repo currently contains only HAR)

- `sql_lab_extractor/__main__.py` — entry point CLI.
- `sql_lab_extractor/config.py` — typed config parsing; env/file references, bukan secret values di args.
- `sql_lab_extractor/auth.py` — browser bootstrap/session import contract; redaction and expiry checks.
- `sql_lab_extractor/client.py` — HTTP client, CSRF/session headers, timeout, retry only for safe polling/fetch.
- `sql_lab_extractor/query.py` — execute/poll/fetch state machine and capability probe.
- `sql_lab_extractor/partition.py` — optional deterministic row/column partitioning; fail closed.
- `sql_lab_extractor/output.py` — streaming CSV/JSONL, schema validation, atomic output.
- `tests/` — unit and sanitized integration fixtures only.
- `pyproject.toml` — package metadata and test command, only if existing environment requires it.
- `.gitignore` — exclude session state, outputs, local config, and secrets.

### Flow

1. Validate target base URL against an explicit allowlist/config; reject arbitrary redirect hosts.
2. Bootstrap authentication interactively with supported browser automation. Preferred handoff is an ephemeral cookie jar plus CSRF token obtained from the authenticated application; do not accept raw HAR as a credential source. Plain HTTP mode is opt-in and must not print credentials.
3. Check session with a harmless authenticated endpoint. Extract CSRF token from response/cookie/header according to actual app behavior.
4. Discover/select `database_id` and schema. Validate SQL input as user-provided authorized SQL; do not concatenate generated predicates into unsafe SQL without a structured, documented contract.
5. Capability probe with a read-only query or user-approved query shape. Request high `queryLimit`, fetch requested rows, record actual `rows`, `limit`, `limitingFactor`, column count, and result key. Never assume request `rows` overrides server policy.
6. Submit setiap page dengan `runAsync=false` sebagai jalur utama. Jika response terminal memuat `status`, `data`, `columns`, dan metadata query lengkap, konsumsi data langsung atau fetch `resultsKey` sesuai kontrak response. Jika submission async/non-terminal, jalankan state machine polling bounded melalui `GET /api/v1/query/updated_since`, selalu filter berdasarkan query identifier atau `resultsKey`, lalu fetch hasil terminal. Feed global tidak pernah menjadi completion signal lintas worker.
7. Fetch results using the server-issued `resultsKey`, not a guessed key. Validate result schema and row/column counts.
8. If complete result is returned, stream output to a temporary file, fsync/close, then atomic rename. Never hold all rows in memory.
9. Jika server cap tetap ada, paginate memakai top-level `ORDER BY` milik caller plus `LIMIT 1000 OFFSET n`. Script hanya memvalidasi keberadaan dan preservasi struktur top-level `ORDER BY`; script tidak meng-inject atau hardcode `assignment_id`, `root.assignment_id`, tie-breaker, atau expression sort lain, dan tidak mencoba membuktikan determinisme semantik. Caller bertanggung jawab penuh atas stabilitas ordering. Ambil `COUNT(*)` sebelum paging dan `COUNT(DISTINCT <declared key>)` hanya bila reconciliation key disediakan; berhenti berdasarkan snapshot count, bukan short page. Full hybrid state machine, results fetch, throttling adaptif, dan fallback range/hash/keyset tersedia sejak rilis pertama, tetapi fallback hanya aktif setelah capability atau performance check membuktikan OFFSET tidak memadai.
10. Emit redacted operational metrics only: query ID, state, durations, counts, cap metadata, and output path; never SQL text, cookies, tokens, returned values, or PII.

## Contracts and Acceptance Criteria

1. CLI accepts a SQL file/stdin, explicit approved base URL, database/schema selection, output format (CSV/JSONL), output path, timeout, and an explicit opt-in for partitioning. Secrets do not appear in process arguments or logs.
2. Authentication supports an interactive browser bootstrap/session handoff; expired or missing session returns a clear Indonesian error and never attempts credential guessing.
3. Client sends required session and `X-CSRFToken` headers where required, handles HTTP status/error JSON, and never retries query execution automatically after an ambiguous timeout.
4. Query state machine distinguishes submitted, running, success, failed, cancelled, timed out, and malformed responses; every terminal state is observable and testable.
5. Capability probe reports actual server row/column limits. It must not claim that UI limits are server limits.
6. Result fetch honors server-issued `resultsKey`; validates declared columns against row records and detects truncation/limiting metadata.
7. Complete results stream to CSV/JSONL with bounded memory (target: memory growth independent of row count for a fixed schema).
8. Default pagination memakai page size 1000, top-level `ORDER BY` caller, dan snapshot-count-bounded OFFSET pages. Validasi hanya memastikan `ORDER BY` ada pada level teratas dan expression-nya dipertahankan; jangan menilai atau menjamin determinisme semantik. Jika reconciliation key dideklarasikan, verifikasi key tersedia, non-null, dan unik sesuai kontrak caller, lalu reconcile expected count, output count, duplicate/missing keys, schema drift, partial failures, dan metadata page. Static extraction window tetap prasyarat OFFSET; jika snapshot berubah, output gagal dan tidak difinalisasi.
9. Interrupted output never replaces an existing completed output. Partial temporary files are identifiable and removable.
10. Tests and fixtures contain no live HAR values, credentials, cookies, CSRF tokens, OIDC artifacts, or PII.
11. Secret scan of source, tests, logs, and git diff finds no token-like live values; logging tests prove SQL/results/session data are redacted.
12. Smoke test against an authorized non-sensitive query proves login/session check, execution, polling, fetch, schema validation, and output end to end. No production mutation or destructive query.

## Pre-mortem

1. **SSO handoff breaks after login UI/MFA change.** Warning: callback succeeds in browser but API receives 401/403 or CSRF mismatch. Mitigation: browser owns login; validate session immediately; versioned adapter and diagnostic redacted status; no password automation fallback by default.
2. **Partitioned output tidak konsisten atau terduplikasi.** Warning: output count berbeda dari snapshot, duplicate reconciliation key, page metadata berubah, atau sinkronisasi data tumpang tindih. Mitigation: caller bertanggung jawab atas `ORDER BY`; script memakai snapshot-count-bounded pages, static-window scheduling, manifest, optional key reconciliation, concurrency reduction, dan atomic finalization hanya setelah semua page sukses.

## Expanded Test Plan

### Unit

- Parse/validate config and allowlisted URLs.
- Redact headers, cookies, CSRF, OIDC fields, SQL, and result values from logs/errors.
- HTTP error and malformed JSON mapping.
- Query state transitions and timeout boundaries.
- Capability interpretation (`NOT_LIMITED`, server cap, unknown).
- CSV/JSONL escaping, nulls, Unicode, schema mismatch, atomic temp output.
- OFFSET page generation, required top-level `ORDER BY` detection/preservation tanpa semantic determinism check, concurrent page scheduling, duplicate page detection, serta range/hash/keyset fallback selection.

### Integration

 - Local fake HTTP server (stdlib) simulates login-established session, CSRF requirement, synchronous execute, async execute with filtered polling fallback, results, 401/403/429/5xx, timeout, malformed payload, and server caps.
- Verify no automatic duplicate execute after network ambiguity.
- Verify streaming output and bounded-memory behavior on generated large fixtures.
- Verify session artifacts are ephemeral/ignored and never included in diagnostic output.

### E2E

 - Against authorized staging or controlled SQL Lab query only: browser SSO bootstrap, harmless metadata discovery, synchronous read-only page execution with bounded multithreading, results fetch, and CSV/JSONL output. Exercise async polling only if the server returns a non-terminal response.
- Explicitly verify >1000 rows and >25 columns only if test dataset/policy permits; confirm the supplied SQL contains a valid top-level `ORDER BY`, preserve qualified sort expressions through joins, and verify any declared reconciliation key separately. The workbook's 14 distinct `link_fasih` UUID suffixes are sample evidence only, not global uniqueness proof.

### Observability

 - Structured redacted events: session check, submit, optional poll state, fetch, row/column counts, cap indicators, duration, retries, final outcome.
- Correlation ID/query ID allowed; SQL text, URL query secrets, cookies, CSRF, OIDC artifacts, and row data forbidden.
 - Metrics: execution duration, optional poll count, fetched rows, fetched columns, page/partition count, concurrency, failure category.

## Risks and Mitigations

- **Credential/session leakage:** browser-managed login; protected ephemeral handoff; secure permissions; no HAR replay; rotate credentials/sessions because supplied HAR contains sensitive live artifacts.
- **Authorization/policy violation:** explicit allowlist and user-owned session; no hard-limit bypass; partitioning only under approved query/data policy.
- **CSRF/session expiry:** immediate authenticated health check, CSRF refresh strategy, bounded re-auth; never replay stale OIDC code.
- **SQL injection in partitioning:** do not string-concatenate untrusted values; require validated key/range representation and documented SQL template; prefer user-supplied complete SQL when partitioning cannot be safely composed.
- **Data corruption:** schema/key reconciliation, manifests, atomic rename, fail closed.
 - **Rate/WAF pressure:** bounded page concurrency (start at 3 workers), no global polling feed shared across workers, exponential backoff only for async polling/fetch where safe, respect `Retry-After`, reduce concurrency on throttling or latency.

## Verification Steps (post-approval)

1. Run static syntax/type checks available in the selected Python setup.
2. Run focused unit tests for redaction, state machine, limits, partition invariants, and streaming output.
3. Run local fake-server integration test.
4. Run smoke CLI with a sanitized fixture and confirm output plus redacted logs.
5. Run authorized staging E2E with a harmless read-only query; record row/column/capability evidence.
6. Inspect changed files and git diff for secrets, placeholders, skipped tests, unsafe retries, and undocumented bypass behavior.
7. Do not claim >1000 rows or >25 columns until a controlled E2E result proves it; if API hard cap exists, report limitation instead of bypassing it.

## ADR

### Decision
Use a hybrid interactive browser session bootstrap with a full synchronous/async API query state machine, capability probe, streaming output, bounded concurrency, and fail-closed output reconciliation. Caller owns semantic ordering correctness.

### Drivers
Security of Keycloak session; actual completeness of SQL results; minimal operational complexity and dependency surface.

### Alternatives considered
Plain HTTP OIDC end-to-end; browser-driven extraction; naïve `rows`/`queryLimit` increases; UUID range/hash pagination; OFFSET pagination; direct database connection.

HAR proves the application API lifecycle and shows API-level row requests beyond the UI, while Keycloak login is dynamic and potentially MFA/WAF-protected. The workbook confirms the output shape and shows 14 distinct assignment UUIDs embedded in `link_fasih`, but does not expose a standalone `assignment_id` column or prove global uniqueness. Hybrid authentication keeps fragile login in the browser. Full API state handling supports terminal synchronous responses plus isolated async polling and results fetch. Given daily synchronization outside the extraction window and an expected maximum near 300,000 rows, caller-supplied top-level `ORDER BY` plus bounded OFFSET remains the default. Script verifies only structural presence and preservation of `ORDER BY`, not semantic determinism. Range/hash/keyset fallback ships in the same design but activates only after capability or performance evidence.

### Consequences
Requires an interactive bootstrap/session handoff and strict local secret handling. Capability may remain limited by server policy; the tool must report/fail rather than bypass. Partitioning adds complexity and is disabled unless prerequisites are explicit. Streaming output supports large results with low memory.

### Follow-ups
Confirm with data/platform owner whether API extraction, >1000 rows, >25 columns, pagination, and local session persistence are authorized. Rotate credentials and invalidate sessions represented in the HAR. Identify a safe staging query/dataset; caller must explicitly accept responsibility for semantic ordering and may declare a separate reconciliation key.

## Consensus Notes

Planner/Architect/Critic provider runs were attempted sequentially. Local proxy returned `404 model not available` for the configured Opus model, so independent expert verdicts were unavailable. Plan incorporates the available sanitized HAR evidence and conservative security/consistency constraints; this is a planning limitation, not an approval.

## Changelog

- Added deliberate pre-mortem and unit/integration/e2e/observability plan.
- Separated UI limits from observed API/server metadata.
 - Added workbook evidence: 14 rows/17 columns and distinct assignment UUID suffixes in `link_fasih`, while marking global uniqueness and source-column identity as unproven.
 - Generalized pagination to require caller-supplied top-level `ORDER BY`; no hardcoded `assignment_id` or unqualified sort column.
 - Clarified that `updated_since` is async/non-terminal fallback only; synchronous page workers do not poll the global feed.
- Selected full hybrid query state machine rather than minimal synchronous-only delivery.
- Removed semantic determinism validation; script validates only top-level `ORDER BY` presence and preservation, while caller owns ordering correctness.
- Added fail-closed rules for row/column partitioning, atomic output, ambiguous execution timeouts, and secret redaction.
