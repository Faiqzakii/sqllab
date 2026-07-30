from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import string
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .artifacts import PageRecord, RunStore
from .auth import AuthError, BrowserSession, bootstrap_browser_session
from .client import HttpClient, redact
from .config import collect_config, parse_args
from .finalize import finalize_run
from .query import QueryError, build_count_query, build_offset_query, execute_sync
from .session import SessionCoordinator, SessionSnapshot

logger = logging.getLogger(__name__)


class _SanitizedRunLogFilter(logging.Filter):
    _sensitive = ("sql=", "cookie=", "csrf", "secret", "rows=", "data=")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        if any(marker in message for marker in self._sensitive):
            record.msg = "[REDACTED LOG MESSAGE]"
            record.args = ()
        return True


def main(arguments: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = parse_args(arguments) or collect_config()
    store: RunStore | None = None
    run_handler: logging.Handler | None = None
    try:
        sql = config.sql_file.read_text(encoding="utf-8")
        store = _select_store(config, sql)
        run_handler = _attach_run_log(store.run_dir)
        store.set_status("running")
        session_dir = Path(config.artifacts_dir) / "session"
        profile_dir = session_dir / "profile"
        coordinator = SessionCoordinator(
            profile_dir=profile_dir,
            lock_path=session_dir / "session.lock",
            refresh=lambda: _load_session(config.base_url, profile_dir),
        )
        records = extract_run(config, sql, coordinator, run_store=store)
        result = finalize_run(store, records, config.final_formats)
        store.set_status(result.status)
    except (AuthError, OSError, QueryError, ValueError, RuntimeError) as error:
        if store is not None:
            store.set_status("failed")
        print(json.dumps(redact({"status": "failed", "error": type(error).__name__, "message": str(error)})), file=sys.stderr)
        return 1
    finally:
        if run_handler is not None:
            logging.getLogger().removeHandler(run_handler)
            run_handler.close()
    print(json.dumps(redact({"status": result.status, "run_dir": str(store.run_dir), "parquet_path": str(result.parquet_path) if result.parquet_path else None, "excel_path": str(result.excel_path) if result.excel_path else None, "rows": result.rows})))
    return 0


def _attach_run_log(run_dir: Path) -> logging.Handler:
    run_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(run_dir / "run.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.addFilter(_SanitizedRunLogFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler


def _select_store(config: Any, sql: str) -> RunStore:
    if config.resume_run is None:
        return RunStore.create(config.artifacts_dir, sql)
    store = RunStore(config.resume_run)
    manifest = store._read_manifest()
    invariants = manifest.get("invariants")
    if not isinstance(invariants, dict) or invariants.get("query_sha256") != _run_invariants(config, sql)["query_sha256"]:
        raise ValueError("Run resume tidak cocok dengan query")
    return store




def _run_invariants(config: Any, sql: str, total_rows: int | None = None) -> dict[str, object]:
    invariants: dict[str, object] = {
        "query_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "page_size": config.page_size,
        "query_limit": config.query_limit,
        "database_id": config.database_id,
        "schema": config.schema,
        "ordering_contract": "deterministic_ordering_required",
    }
    if total_rows is not None:
        invariants["total_rows"] = total_rows
    return invariants


def _validate_or_record_invariants(store: RunStore, config: Any, sql: str, total_rows: int) -> None:
    expected = _run_invariants(config, sql, total_rows)
    manifest = store._read_manifest()
    existing = manifest.get("invariants", {})
    if not isinstance(existing, dict):
        raise ValueError("Manifest invariants tidak valid")
    for name, value in expected.items():
        if name in existing and existing[name] != value:
            raise ValueError(f"Invariant {name!r} tidak cocok: lama={existing[name]!r} baru={value!r}")
    for name, value in expected.items():
        store.record_invariant(name, value)


PAGE_START_INTERVAL_SECONDS = 2.1


def _wait_for_page_slot(last_started_at: float | None, clock: Any = time.monotonic, sleep: Any = time.sleep) -> float:
    if last_started_at is not None:
        remaining = PAGE_START_INTERVAL_SECONDS - (clock() - last_started_at)
        if remaining > 0:
            sleep(remaining)
    return clock()


def extract_run(
    config: Any,
    sql: str,
    coordinator: SessionCoordinator,
    client_factory: Any = None,
    run_store: RunStore | None = None,
) -> list[PageRecord]:
    """Execute missing synchronous pages and retain auditable page artifacts."""
    store = run_store or RunStore.create(config.artifacts_dir, sql)
    make_client = client_factory or _client_for_snapshot(config)
    store.append_event({"event": "run_started"})
    started = time.monotonic()
    try:
        count_state = _execute_with_refresh(config, coordinator, make_client, build_count_query(sql), stage="count")
    except BaseException as error:
        store.record_run_failure("count", error)
        raise
    total_rows = _total_rows(count_state)
    _validate_or_record_invariants(store, config, sql, total_rows)
    offsets = range(0, total_rows, config.page_size)
    total_pages = len(range(0, total_rows, config.page_size))
    records: list[PageRecord] = []
    gaps: list[int] = []
    for offset in offsets:
        record = store.validate_page_record(offset)
        if record is None:
            gaps.append(offset)
        else:
            records.append(record)
    store.append_event({"event": "count_completed", "rows": total_rows, "elapsed_ms": int((time.monotonic() - started) * 1000), "completed": len(records), "total": total_rows})
    logger.info("event=count_completed rows=%d total_pages=%d elapsed_ms=%d", total_rows, total_pages, int((time.monotonic() - started) * 1000))
    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        pending = iter(gaps)
        futures: dict[Future[list[dict[str, Any]]], tuple[int, float]] = {}
        last_page_started_at: float | None = None

        def start_next_page() -> bool:
            nonlocal last_page_started_at
            try:
                offset = next(pending)
            except StopIteration:
                return False
            page_started = _wait_for_page_slot(last_page_started_at)
            last_page_started_at = page_started
            store.append_event({"event": "page_started", "offset": offset, "attempt": 1, "completed": len(records), "total": total_rows})
            logger.info("event=page_started offset=%d limit=%d completed=%d total_pages=%d", offset, config.page_size, len(records), total_pages)
            futures[executor.submit(_fetch_sync_page, offset, config, sql, coordinator, make_client)] = (offset, page_started)
            return True

        for _ in range(min(config.workers, len(gaps))):
            start_next_page()
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                offset, page_started = futures.pop(future)
                try:
                    rows = future.result()
                    record = store.write_page(offset, rows)
                except BaseException as error:
                    store.record_failure(offset, 1, error)
                    raise
                records.append(record)
                store.append_event({"event": "page_completed", "offset": offset, "rows": record.rows, "attempt": 1, "elapsed_ms": int((time.monotonic() - page_started) * 1000), "completed": len(records), "total": total_rows})
                logger.info("event=page_completed offset=%d rows=%d elapsed_ms=%d completed=%d total_pages=%d", offset, record.rows, int((time.monotonic() - page_started) * 1000), len(records), total_pages)
                start_next_page()
    expected_offsets = tuple(offsets)
    valid_records = {record.offset: record for record in records}
    if len(valid_records) != len(expected_offsets) or any(offset not in valid_records or store.validate_page_record(offset) is None for offset in expected_offsets):
        raise RuntimeError("Ekstraksi memiliki halaman yang belum lengkap")
    return sorted(records, key=lambda record: record.offset)


def _fetch_sync_page(offset: int, config: Any, sql: str, coordinator: SessionCoordinator, make_client: Any) -> list[dict[str, Any]]:
    state = _execute_with_refresh(config, coordinator, make_client, build_offset_query(sql, config.page_size, offset), stage=f"page:{offset}")
    if state.data is None:
        raise QueryError(f"Hasil page OFFSET {offset} tidak tersedia")
    return state.data


def _execute_with_refresh(config: Any, coordinator: SessionCoordinator, make_client: Any, sql: str, stage: str = "query") -> Any:
    """Execute once; POST execution is not idempotent and must never be replayed."""
    snapshot = coordinator.get_snapshot()
    payload = build_execute_payload(config, sql)
    return execute_sync(make_client(snapshot), payload, stage=stage)


def _total_rows(state: Any) -> int:
    if not state.data or "total_rows" not in state.data[0]:
        raise QueryError("Hasil COUNT(*) tidak valid")
    try:
        return int(state.data[0]["total_rows"])
    except (TypeError, ValueError) as error:
        raise QueryError("Hasil COUNT(*) bukan angka") from error


def _client_for_snapshot(config: Any):
    def create(snapshot: SessionSnapshot) -> HttpClient:
        return HttpClient(config.base_url, snapshot.session.csrf_token, snapshot.session.cookie_header)

    return create
def build_execute_payload(config: Any, sql: str) -> dict[str, Any]:
    alphabet = string.ascii_letters + string.digits + "_-"
    client_id = "".join(secrets.choice(alphabet) for _ in range(10))
    return {
        "client_id": client_id,
        "database_id": config.database_id,
        "json": True,
        "runAsync": False,
        "schema": config.schema,
        "sql": sql,
        "sql_editor_id": config.sql_editor_id,
        "tab": config.tab,
        "tmp_table_name": "",
        "select_as_cta": False,
        "ctas_method": "TABLE",
        "queryLimit": config.query_limit,
        "expand_data": True,
    }


def _load_session(base_url: str, profile_dir: Any = None) -> BrowserSession:
    csrf_token = os.environ.get("SQL_LAB_CSRF_TOKEN")
    cookie = os.environ.get("SQL_LAB_COOKIE")
    if csrf_token and cookie:
        return BrowserSession(cookie, csrf_token)
    return bootstrap_browser_session(base_url, profile_dir=profile_dir)

if __name__ == "__main__":
    raise SystemExit(main())
