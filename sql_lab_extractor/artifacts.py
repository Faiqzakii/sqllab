from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as parquet

from .client import HttpResponseError, HttpStatusError, HttpTimeoutError, redact

logger = logging.getLogger(__name__)

RUN_DIR_PATTERN = re.compile(r"run-\d{8}T\d{6}Z-[0-9a-f]{8}")

@dataclass(frozen=True)
class PageRecord:
    offset: int
    rows: int
    columns: tuple[str, ...]
    checksum: str


class RunStore:
    """Per-run atomic Parquet page store with legacy CSV resume support."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self._pages_dir = run_dir / "pages"
        self._failures_dir = run_dir / "failures"
        self._progress_path = run_dir / "progress.jsonl"
        self._manifest_path = run_dir / "manifest.json"
        self._progress_lock = threading.Lock()
        self._manifest_lock = threading.Lock()

    @classmethod
    def create(cls, root: Path, sql: str) -> "RunStore":
        if not sql:
            raise ValueError("SQL wajib diisi untuk membuat run")
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for _ in range(10):
            candidate = root / f"run-{timestamp}-{secrets.token_hex(4)}"
            try:
                candidate.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Gagal membuat direktori run yang unik")
        (candidate / "pages").mkdir()
        (candidate / "failures").mkdir()
        (candidate / "query.sql").write_text(sql, encoding="utf-8")
        store = cls(candidate)
        manifest = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "created",
            "pages": {},
        }
        store._write_json_atomic(store._manifest_path, manifest)
        store._progress_path.touch()
        return store

    def record_config(self, config: Any) -> None:
        """Persist non-secret effective settings needed for short resume."""
        self._update_manifest({"config": {
            "base_url": config.base_url,
            "database_id": config.database_id,
            "schema": config.schema,
            "page_size": config.page_size,
            "query_limit": config.query_limit,
            "sql_editor_id": config.sql_editor_id,
            "tab": config.tab,
            "workers": config.workers,
            "final_formats": list(config.final_formats),
        }})

    def append_event(self, event: dict[str, object]) -> None:
        """Append one redacted JSON object per line; serialized under one lock."""
        line = json.dumps(redact(event), ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._progress_lock:
            with open(self._progress_path, "a", encoding="utf-8", newline="") as progress:
                progress.write(line)
                progress.flush()
                os.fsync(progress.fileno())

    def write_page(self, offset: int, rows: Iterable[dict[str, object]]) -> PageRecord:
        """Write one schema-validated Parquet page atomically."""
        if offset < 0:
            raise ValueError("Offset halaman wajib non-negatif")
        materialized = list(rows)
        first_row = materialized[0] if materialized else None
        columns = self._page_columns(first_row)
        expected = set(columns)
        for row in materialized:
            if set(row) != expected:
                raise ValueError("Schema drift terdeteksi dalam satu halaman")
        target = self._page_path(offset)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=self._pages_dir)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            schema = pa.schema([pa.field(column, pa.string()) for column in columns])
            table = pa.Table.from_pylist([{column: None if row.get(column) is None else str(row[column]) for column in columns} for row in materialized], schema=schema)
            parquet.write_table(table, temporary)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        record = PageRecord(offset=offset, rows=len(materialized), columns=columns, checksum=_checksum_file(target))
        self._record_page_in_manifest(record)
        return record

    def validate_page_record(self, offset: int) -> PageRecord | None:
        """Validate current Parquet pages and legacy CSV pages."""
        record = self._manifest_page_record(offset)
        if record is None:
            return None
        target = self._page_path(offset)
        legacy = self._legacy_page_path(offset)
        try:
            if target.is_file():
                if _checksum_file(target) != record.checksum:
                    return None
                table = parquet.read_table(target)
                if tuple(table.column_names) != record.columns or table.num_rows != record.rows:
                    return None
                return record
            if legacy.is_file():
                if _checksum_file(legacy) != record.checksum:
                    return None
                with legacy.open("r", encoding="utf-8", newline="") as page:
                    reader = csv.reader(page)
                    if tuple(next(reader, ())) != record.columns or sum(1 for _ in reader) != record.rows:
                        return None
                return record
        except (OSError, csv.Error, ValueError, pa.ArrowException):
            return None
        return None

    def load_valid_page(self, offset: int) -> list[dict[str, object]] | None:
        record = self.validate_page_record(offset)
        if record is None:
            return None
        target = self._page_path(offset)
        if target.is_file():
            return parquet.read_table(target).to_pylist()
        with self._legacy_page_path(offset).open("r", encoding="utf-8", newline="") as page:
            return list(csv.DictReader(page))

    def set_status(self, status: str) -> None:
        """Atomically update the run status without discarding manifest fields."""
        self._update_manifest({"status": status})

    def record_invariant(self, name: str, value: object) -> None:
        """Atomically record one immutable run invariant."""
        if not name:
            raise ValueError("Nama invariant wajib diisi")
        with self._manifest_lock:
            manifest = self._read_manifest()
            invariants = manifest.setdefault("invariants", {})
            if not isinstance(invariants, dict):
                raise ValueError("Manifest invariants tidak valid")
            if name in invariants and invariants[name] != value:
                raise ValueError(f"Invariant {name!r} sudah memiliki nilai berbeda")
            invariants[name] = value
            self._write_json_atomic(self._manifest_path, manifest)



    def record_failure(self, offset: int, attempt: int, error: object) -> None:
        """Persist a sanitized failure record and a progress event; never raises."""
        try:
            sanitized = _sanitize_failure(error)
            payload = {
                "offset": offset,
                "attempt": attempt,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "error": sanitized,
            }
            self._write_json_atomic(self._failures_dir / f"offset-{offset}.json", payload)
            self.append_event(
                {
                    "event": "page_failed",
                    "offset": offset,
                    "attempt": attempt,
                    "error_type": sanitized["type"],
                    "error": sanitized["message"],
                }
            )
        except Exception:
            logger.exception("Failed to persist sanitized failure record at offset %d", offset)

    def record_run_failure(self, stage: str, error: object) -> None:
        """Persist a sanitized failure that occurs before page scheduling."""
        sanitized = _sanitize_failure(error)
        payload = {
            "stage": stage,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "error": sanitized,
        }
        self._write_json_atomic(self._failures_dir / f"run-{stage}.json", payload)
        self.append_event({"event": "run_failed", "stage": stage, "error_type": sanitized["type"]})

    def _page_path(self, offset: int) -> Path:
        return self._pages_dir / f"offset-{offset}.parquet"

    def _legacy_page_path(self, offset: int) -> Path:
        return self._pages_dir / f"offset-{offset}.csv"


    @staticmethod
    def _page_columns(first_row: dict[str, object] | None) -> tuple[str, ...]:
        if first_row is None:
            return ()
        columns = tuple(first_row)
        if not columns:
            raise ValueError("Baris halaman wajib memiliki minimal satu kolom")
        return columns

    def _read_manifest(self) -> dict[str, object]:
        return json.loads(self._manifest_path.read_text(encoding="utf-8"))

    def _update_manifest(self, updates: dict[str, object]) -> None:
        with self._manifest_lock:
            manifest = self._read_manifest()
            manifest.update(updates)
            self._write_json_atomic(self._manifest_path, manifest)
    def _manifest_page_record(self, offset: int) -> PageRecord | None:
        try:
            manifest = self._read_manifest()
        except (OSError, json.JSONDecodeError):
            return None
        entry = manifest.get("pages", {}).get(str(offset))
        if not isinstance(entry, dict):
            return None
        try:
            return PageRecord(
                offset=offset,
                rows=int(entry["rows"]),
                columns=tuple(entry["columns"]),
                checksum=str(entry["checksum"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _record_page_in_manifest(self, record: PageRecord) -> None:
        # The manifest update is a read-modify-write transaction; atomic replace
        # alone cannot preserve simultaneous updates from multiple page workers.
        with self._manifest_lock:
            manifest = self._read_manifest()
            manifest.setdefault("pages", {})[str(record.offset)] = {
                "format": "parquet",
                "rows": record.rows,
                "columns": list(record.columns),
                "checksum": record.checksum,
            }
            self._write_json_atomic(self._manifest_path, manifest)

    @staticmethod
    def _write_json_atomic(target: Path, payload: dict[str, object]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


class _HashingWriter:
    """File-like wrapper that feeds every written string into a digest."""

    def __init__(self, wrapped: Any, digest: Any):
        self._wrapped = wrapped
        self._digest = digest

    def write(self, text: str) -> int:
        self._digest.update(text.encode("utf-8"))
        return self._wrapped.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def _sanitize_failure(error: object) -> dict[str, object]:
    """Return bounded structured diagnostics without authentication secrets."""
    if isinstance(error, (HttpStatusError, HttpResponseError, HttpTimeoutError)):
        return {
            "type": type(error).__name__,
            "category": "failure",
            "message": "error recorded",
            **redact(error.diagnostic),
        }
    return {
        "type": "dict" if isinstance(error, dict) else type(error).__name__,
        "category": "failure",
        "message": "error recorded",
    }

def _checksum_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
