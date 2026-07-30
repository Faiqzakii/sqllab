# SQL Lab Reference Integration Design

## Goal

Integrate proven `bpsktt/sqllab.py` execution patterns into the existing CLI.
Preserve Camoufox authentication, safe logging, deterministic pagination,
resume support, and bounded memory.

## Session

Use a persistent Camoufox profile under `artifacts/session/profile/`, shared
across runs and workers. A cross-process `session.lock` serializes login or
session refresh. Workers consume one in-memory session snapshot. When a request
proves the session invalid, one coordinator refreshes it; other workers wait and
use the new generation. Ambiguous execute timeouts are never retried
automatically.

## Run artifacts

Each run uses `artifacts/run-<UTC timestamp>-<short id>/` with `query.sql`,
`manifest.json`, `progress.jsonl`, `run.log`, `pages/offset-<n>.csv`,
`failures/offset-<n>.json`, and final `result.parquet` then `result.xlsx`. SQL is
stored only in `query.sql`; logs and progress events exclude cookies, CSRF,
credentials, headers, response bodies, and result rows.

Each progress event is one JSON object containing event, offset, attempt, row
count, elapsed time, and error type/message where applicable. Successful pages
include checksum, schema, and row count for resume validation.

## Extraction

Execute synchronous SQL Lab requests using the successful `bpsktt` payload
shape. Do not poll. Schedule offsets through a bounded configurable worker pool.
Each page writes its own atomic CSV. Completion logging follows actual worker
completion, while final merge follows numeric offset order. Missing or invalid
pages prevent a successful final result.

Column chunking is optional and fail-closed: split only when top-level SQL
parsing identifies a stable explicit key that is present and unique in every
chunk. Never merge by row index and never invent `ORDER BY`.

## Outputs

Validate all page CSV files, write Parquet first through `pyarrow`, then generate
Excel through `openpyxl`. Excel uses multiple sheets when row limits require it.
Parquet remains valid if Excel generation fails. CSV pages remain for audit and
resume.

## Artifact policy

This is local-first. Enforce artifact safety through `.gitignore`: ignore
`artifacts/`, `.env`, HAR files, logs, partial files, and Python caches. Do not
add deployment-only path policy.

## Verification

Add deterministic unit tests for lock coordination, session generation
invalidation, progress JSONL events, page resume/checksum validation, ordered
merge, Parquet-first output, Excel sheet splitting, and failed-page reporting.
Live SSO/MFA remains an authorized manual verification step.
