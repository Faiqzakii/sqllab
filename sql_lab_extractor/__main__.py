from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .artifacts import PageRecord, RunStore
from .auth import AuthError, BrowserSession, bootstrap_browser_session
from .client import HttpClient, redact
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
    """Fetch bounded page batches until an empty page, reusing valid artifacts."""
    store = run_store or RunStore.create(config.artifacts_dir, sql)
    make_client = client_factory or _client_for_snapshot(config)
    store.append_event({"event": "run_started", "pagination": "until-empty"})
    _validate_or_record_invariants(store, config, sql)
    records: list[PageRecord] = []
    offset = 0
    terminal_offset: int | None = None
    while terminal_offset is None:
        offsets = [offset + index * config.page_size for index in range(config.workers)]
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            futures = {candidate: executor.submit(_fetch_sync_page, candidate, config, sql, coordinator, make_client) for candidate in offsets}
            for candidate in offsets:
                existing = store.validate_page_record(candidate)
                if existing is not None:
                    record = existing
                else:
                    try:
                        record = store.write_page(candidate, futures[candidate].result())
                    except BaseException as error:
                        store.record_failure(candidate, 1, error)
                        raise
                if terminal_offset is None:
                    records.append(record)
                if record.rows == 0 and terminal_offset is None:
                    terminal_offset = candidate
        if terminal_offset is None:
            offset += config.workers * config.page_size
    store.record_invariant("terminal_offset", terminal_offset)
    return sorted((record for record in records if record.offset <= terminal_offset), key=lambda record: record.offset)


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
