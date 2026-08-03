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
from .client import HttpClient, HttpStatusError, redact
from .config import collect_config, parse_args
from .finalize import finalize_run
from .query import QueryError, build_offset_query, execute_sync
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
        store.record_config(config)
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


def _validate_or_record_invariants(store: RunStore, config: Any, sql: str) -> None:
    expected = _run_invariants(config, sql)
    manifest = store._read_manifest()
    existing = manifest.get("invariants", {})
    if not isinstance(existing, dict):
        raise ValueError("Manifest invariants tidak valid")
    for name, value in expected.items():
        if name in existing and existing[name] != value:
            raise ValueError(f"Invariant {name!r} tidak cocok")
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
    """Keep a bounded request window until consecutive empty pages confirm terminal."""
    store = run_store or RunStore.create(config.artifacts_dir, sql)
    make_client = client_factory or _client_for_snapshot(config)
    page_size = config.page_size
    store.append_event({"event": "run_started", "pagination": "sliding-until-empty"})
    _validate_or_record_invariants(store, config, sql)
    started = time.monotonic()
    records: dict[int, PageRecord] = {}
    outcomes: dict[int, str] = {}
    failed_offsets: set[int] = set()
    next_offset = 0
    evaluation_offset = 0
    empty_streak = 0
    terminal_offset: int | None = None
    last_page_started_at: float | None = None
    consecutive_failures = 0
    last_error: BaseException | None = None
    fatal_error: BaseException | None = None
    empty_probe_active = False

    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        active: dict[Future[list[dict[str, Any]]], tuple[int, float]] = {}
        while terminal_offset is None:
            while len(active) < config.workers and consecutive_failures == 0 and fatal_error is None and not empty_probe_active:
                existing = store.validate_page_record(next_offset)
                if existing is not None:
                    records[next_offset] = existing
                    outcomes[next_offset] = "empty" if existing.rows == 0 else "data"
                    store.append_event({"event": "page_reused", "offset": next_offset, "rows": existing.rows})
                    next_offset += page_size
                    while evaluation_offset in outcomes:
                        outcome = outcomes[evaluation_offset]
                        empty_streak = empty_streak + 1 if outcome == "empty" else 0
                        evaluation_offset += page_size
                        if empty_streak == config.workers:
                            terminal_offset = evaluation_offset - config.workers * page_size
                            break
                    if terminal_offset is not None:
                        break
                    continue
                page_started = _wait_for_page_slot(last_page_started_at)
                last_page_started_at = page_started
                store.append_event({"event": "page_started", "offset": next_offset, "attempt": 1})
                future = executor.submit(_fetch_sync_page, next_offset, config, sql, coordinator, make_client)
                active[future] = (next_offset, page_started)
                next_offset += page_size
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                candidate, page_started = active.pop(future)
                try:
                    rows = future.result()
                except BaseException as error:
                    failed_offsets.add(candidate)
                    outcomes[candidate] = "failed"
                    store.record_failure(candidate, 1, error)
                    consecutive_failures += 1
                    last_error = error
                    if isinstance(error, HttpStatusError) and error.status_code == 429:
                        fatal_error = error
                        store.append_event({"event": "run_rate_limited", "offset": candidate, "status_code": 429})
                        for pending in active:
                            pending.cancel()
                        break
                    continue
                consecutive_failures = 0
                record = store.write_page(candidate, rows)
                records[candidate] = record
                outcomes[candidate] = "empty" if record.rows == 0 else "data"
                if record.rows == 0:
                    empty_probe_active = True
                event = "page_empty" if record.rows == 0 else "page_completed"
                store.append_event({"event": event, "offset": candidate, "rows": record.rows, "attempt": 1, "elapsed_ms": int((time.monotonic() - page_started) * 1000)})
            if fatal_error is not None:
                raise fatal_error
            while evaluation_offset in outcomes:
                outcome = outcomes[evaluation_offset]
                empty_streak = empty_streak + 1 if outcome == "empty" else 0
                evaluation_offset += page_size
                if empty_streak == config.workers:
                    terminal_offset = evaluation_offset - config.workers * page_size
                    break
            if empty_probe_active and not active and terminal_offset is None:
                empty_probe_active = False
            if consecutive_failures >= config.workers and last_error is not None:
                raise last_error

    retry_offsets = sorted(candidate for candidate in failed_offsets if candidate < terminal_offset)
    for candidate in retry_offsets:
        store.append_event({"event": "page_retry_started", "offset": candidate, "attempt": 2})
        try:
            record = store.write_page(candidate, _fetch_sync_page(candidate, config, sql, coordinator, make_client))
        except BaseException as error:
            store.record_failure(candidate, 2, error)
            continue
        records[candidate] = record
        failed_offsets.remove(candidate)
        store.append_event({"event": "page_completed", "offset": candidate, "rows": record.rows, "attempt": 2})

    expected_offsets = tuple(range(0, terminal_offset, page_size))
    missing_offsets = [candidate for candidate in expected_offsets if candidate not in records or records[candidate].rows == 0]
    if missing_offsets:
        store.append_event({"event": "run_incomplete", "missing_offsets": missing_offsets})
        raise RuntimeError(f"Ekstraksi belum lengkap; ulangi run untuk {len(missing_offsets)} offset gagal atau hilang")
    store.record_invariant("terminal_offset", terminal_offset)
    store.append_event({"event": "terminal_confirmed", "offset": terminal_offset, "empty_pages": config.workers})
    result = [records[candidate] for candidate in expected_offsets]
    total_rows = sum(record.rows for record in result)
    store.append_event({"event": "pages_completed", "rows": total_rows, "pages": len(result), "elapsed_ms": int((time.monotonic() - started) * 1000)})
    return result


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
