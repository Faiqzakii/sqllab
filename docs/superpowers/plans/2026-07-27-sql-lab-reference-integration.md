# SQL Lab Reference Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent shared sessions, resumable per-run artifacts,
synchronous parallel extraction, and Parquet-first plus Excel output.

**Architecture:** A session coordinator owns one persistent Camoufox profile and
publishes immutable session generations to workers. A run store writes atomic
page CSV files and append-only progress events. Extraction uses bounded workers
and synchronous SQL Lab responses; finalization validates ordered pages, writes
Parquet, then Excel.

**Tech Stack:** Python standard library, Camoufox, pyarrow, openpyxl, unittest.

## Global Constraints

- Never log credentials, cookies, CSRF values, request headers, SQL, or rows.
- Store SQL only in ignored per-run `query.sql`.
- Never retry an ambiguous execute timeout.
- Never invent pagination ordering or merge column chunks by row index.
- Keep page processing bounded by configured worker and page sizes.
- User-facing errors use Indonesian; diagnostic logs use English.
- Live SSO/MFA requires authorized credentials and interactive MFA.

---

### Task 1: Runtime configuration and ignored artifacts

**Files:**

- Modify: `.gitignore`
- Modify: `sql_lab_extractor/config.py`
- Modify: `tests/test_extractor.py`

**Interfaces:**

- Produces: `Config.artifacts_dir: Path`, `Config.final_formats: tuple[str, ...]`
- Produces: CLI flags `--artifacts-dir` and `--final-format`

- [ ] **Step 1: Write failing configuration tests**

Add tests asserting default `artifacts_dir == Path("artifacts")`, default formats
are `("parquet", "xlsx")`, explicit format selection is accepted, and invalid
formats fail. Extend ignore assertion with `artifacts/`, `*.log`, and `*.har`.

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m unittest tests.test_extractor.ConfigTests -v
```

Expected: FAIL because new fields and arguments do not exist.

- [ ] **Step 3: Implement minimal configuration**

Add immutable fields, parser arguments, interactive prompts, and validation.
Keep page CSV an internal artifact rather than a final format choice.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 command. Expected: all `ConfigTests` pass.

### Task 2: Persistent profile and cross-process session lock

**Files:**

- Create: `sql_lab_extractor/session.py`
- Modify: `sql_lab_extractor/auth.py`
- Modify: `tests/test_extractor.py`

**Interfaces:**

- Produces: `SessionSnapshot(generation: int, session: BrowserSession)`
- Produces: `SessionCoordinator(profile_dir, lock_path, refresh)`
- Produces: `get_snapshot() -> SessionSnapshot`
- Produces: `invalidate(generation: int) -> SessionSnapshot`
- Consumes: `bootstrap_browser_session(base_url, profile_dir=...)`

- [ ] **Step 1: Write failing coordinator tests**

Use temporary directories and threads. Assert concurrent invalidations for one
generation invoke `refresh` once, all waiters receive the same higher generation,
and invalidating an old generation does not refresh again. Assert default profile
resolves to `artifacts/session/profile` rather than a temporary directory.

- [ ] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_extractor.SessionCoordinatorTests -v
```

Expected: FAIL because `session.py` and injectable profile support do not exist.

- [ ] **Step 3: Implement coordinator and lock**

Use a process-local `threading.Condition` plus an atomic lock file acquired with
`os.open(..., os.O_CREAT | os.O_EXCL)`. Store only non-secret generation metadata
in `generation.json`. Use bounded stale-lock recovery based on PID and timestamp;
never serialize cookie or CSRF values. Pass explicit persistent profile path into
Camoufox and stop deleting it after browser close.

- [ ] **Step 4: Verify GREEN**

Run Task 2 tests and existing `AuthTests`. Expected: pass.

### Task 3: Per-run store and append-only progress

**Files:**

- Create: `sql_lab_extractor/artifacts.py`
- Modify: `tests/test_extractor.py`

**Interfaces:**

- Produces: `RunStore.create(root: Path, sql: str) -> RunStore`
- Produces: `append_event(event: dict[str, object]) -> None`
- Produces: `write_page(offset, rows) -> PageRecord`
- Produces: `load_valid_page(offset) -> list[dict[str, object]] | None`
- Produces: `record_failure(offset, attempt, error) -> None`
- `PageRecord` contains offset, row count, columns, and SHA-256 checksum.

- [ ] **Step 1: Write failing run-store tests**

Assert unique run directory naming, exact SQL storage, one valid JSON object per
progress line, atomic page CSV creation, checksum/schema validation, invalid page
rejection, and sanitized failure JSON without known secret values.

- [ ] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_extractor.RunStoreTests -v
```

Expected: FAIL because run store does not exist.

- [ ] **Step 3: Implement minimal run store**

Use `csv.DictWriter`, `hashlib.sha256`, `.partial`, `fsync`, and `os.replace`.
Serialize progress writes under one lock. Apply existing recursive `redact()` to
errors before persistence. Write manifest updates atomically.

- [ ] **Step 4: Verify GREEN**

Run Task 3 tests. Expected: pass.

### Task 4: Synchronous parallel page extraction and session refresh barrier

**Files:**

- Modify: `sql_lab_extractor/query.py`
- Modify: `sql_lab_extractor/client.py`
- Modify: `sql_lab_extractor/__main__.py`
- Modify: `tests/test_extractor.py`

**Interfaces:**

- Produces: `execute_sync(client, payload) -> QueryState`
- Produces: `extract_run(coordinator, config, sql, run_store) -> list[PageRecord]`
- Consumes: Task 2 `SessionCoordinator`, Task 3 `RunStore`.

- [ ] **Step 1: Write failing synchronous execution tests**

Assert a successful direct response returns data without polling; a running
response fails clearly because polling is disabled; request timeout is not
retried. Assert pages finishing out of order create completion events immediately
but return records sorted by offset.

- [ ] **Step 2: Write failing refresh-barrier tests**

Simulate two workers receiving authenticated 401/403 responses from one session
generation. Assert one refresh occurs and safe GET/session-validation work may
continue, while an ambiguous POST execute timeout is recorded as failed without
retry.

- [ ] **Step 3: Verify RED**

```powershell
python -m unittest tests.test_extractor.SynchronousExtractionTests -v
```

Expected: FAIL because synchronous-only orchestration and coordinator integration
do not exist.

- [ ] **Step 4: Implement synchronous extraction**

Remove extraction-path polling. Submit offsets with `ThreadPoolExecutor` and
consume `as_completed` for real-time progress. Each worker writes its page through
`RunStore`; coordinator returns numeric offset order. Keep current count-first and
top-level deterministic `ORDER BY` checks.

- [ ] **Step 5: Verify GREEN**

Run Task 4 tests plus `QueryTests`. Expected: pass.

### Task 5: Safe column chunking

**Files:**

- Modify: `sql_lab_extractor/query.py`
- Modify: `tests/test_extractor.py`

**Interfaces:**

- Produces: `build_column_chunks(sql, max_columns, key_column) -> list[str]`
- Produces: `merge_column_pages(chunks, key_column) -> list[dict[str, object]]`

- [ ] **Step 1: Write failing chunk tests**

Cover quoted strings, nested expressions, aliases, explicit stable key inclusion,
unique-key validation, duplicate-key rejection, and refusal to split `SELECT *`,
CTEs, or unparseable SQL without verified metadata. Assert no index-based merge.

- [ ] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_extractor.ColumnChunkTests -v
```

Expected: FAIL because APIs do not exist.

- [ ] **Step 3: Implement fail-closed splitter**

Reuse top-level token scanning already in `query.py`. Split only explicit select
lists. Require caller-provided `key_column`; include it in every chunk and merge
through a dictionary keyed by its unique value. Reject duplicates or missing keys.

- [ ] **Step 4: Verify GREEN**

Run Task 5 tests. Expected: pass.

### Task 6: Parquet-first and Excel finalization

**Files:**

- Create: `sql_lab_extractor/finalize.py`
- Modify: `sql_lab_extractor/__main__.py`
- Modify: `tests/test_extractor.py`
- Create or modify: dependency manifest used by this project

**Interfaces:**

- Produces: `finalize_run(run_store, records, formats) -> FinalizeResult`
- `FinalizeResult` contains status, parquet path, Excel path, rows, and columns.

- [ ] **Step 1: Write failing finalization tests**

Using small page CSV fixtures, assert offset-ordered Parquet rows, schema drift
rejection, Parquet creation before Excel invocation, Excel multi-sheet boundaries
through an injectable low row limit, and `completed_with_excel_error` preserving
Parquet when Excel fails.

- [ ] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_extractor.FinalizationTests -v
```

Expected: FAIL because finalization module does not exist.

- [ ] **Step 3: Add minimal dependencies and implementation**

Use `pyarrow.csv.open_csv` or bounded record batches to write one Parquet writer.
Use openpyxl write-only workbooks and stream Parquet batches into sheets. Never
load all rows into pandas or a Python list.

- [ ] **Step 4: Verify GREEN**

Run Task 6 tests. Expected: pass.

### Task 7: CLI integration, run logging, and resume behavior

**Files:**

- Modify: `sql_lab_extractor/__main__.py`
- Modify: `sql_lab_extractor/auth.py`
- Modify: `tests/test_extractor.py`

**Interfaces:**

- Main creates `RunStore`, `SessionCoordinator`, executes pages, finalizes output,
  and emits one sanitized completion JSON object.

- [ ] **Step 1: Write failing CLI integration tests**

Patch only browser/network boundaries. Assert run creation, persistent profile,
progress events, skipped valid pages on resume, failed-page nonzero exit, Parquet
path in completion output, and Excel degradation status.

- [ ] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_extractor.CliIntegrationTests -v
```

Expected: FAIL because main still writes one direct CSV/JSONL target.

- [ ] **Step 3: Implement orchestration**

Replace direct `AtomicWriter` flow with run store, coordinator, extraction, and
finalization. Configure rotating `run.log` inside the run directory. Keep browser
log sanitized and move its default path under the active run when available.

- [ ] **Step 4: Verify GREEN**

Run Task 7 tests. Expected: pass.

### Task 8: Full verification and authorized smoke test

**Files:**

- Modify only files required by verified failures.

- [ ] **Step 1: Run full automated suite**

```powershell
python -m unittest discover -s tests -v
python -m compileall -q sql_lab_extractor tests
```

Expected: all tests pass; compile command exits zero.

- [ ] **Step 2: Run local dependency imports**

```powershell
python -c "import camoufox, pyarrow, openpyxl; print('runtime dependencies OK')"
```

Expected: `runtime dependencies OK`.

- [ ] **Step 3: Run authorized smoke extraction**

With authorized credentials, MFA, and VPN, execute a deterministic query returning
more than one page. Verify concurrent page completion in `progress.jsonl`, valid
page checksums, `result.parquet`, `result.xlsx`, and no secret values in either
log. If authorization prerequisites are unavailable, report this exact manual
verification as blocked without claiming live success.
