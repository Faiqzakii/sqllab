from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable

DEFAULT_BASE_URL = "https://fasih-dashboard.bps.go.id"
DEFAULT_DATABASE_ID = 25
DEFAULT_SCHEMA = "tcz_37526b20"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_QUERY_LIMIT = 100000
DEFAULT_SQL_EDITOR_ID = "951872"
DEFAULT_TAB = "UB_Nilai Tambah"
DEFAULT_WORKERS = 2
MAX_WORKERS = 8
MAX_PAGE_SIZE = 10_000
DEFAULT_ARTIFACTS_DIR = Path("artifacts")
DEFAULT_FINAL_FORMATS = ("parquet", "xlsx")
FINAL_FORMAT_CHOICES = ("parquet", "xlsx")


@dataclass(frozen=True)
class Config:
    base_url: str
    database_id: int
    schema: str
    sql_file: Path
    page_size: int
    query_limit: int
    sql_editor_id: str
    tab: str
    workers: int
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR
    final_formats: tuple[str, ...] = DEFAULT_FINAL_FORMATS
    resume_run: Path | None = None


def parse_args(arguments: list[str] | None = None) -> Config | None:
    parser = argparse.ArgumentParser(description="Authorized SQL Lab extractor")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--database-id", default=DEFAULT_DATABASE_ID, type=int)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--sql-file", type=Path)
    parser.add_argument("--resume-run", type=Path)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--query-limit", type=int, default=DEFAULT_QUERY_LIMIT)
    parser.add_argument("--sql-editor-id", default=DEFAULT_SQL_EDITOR_ID)
    parser.add_argument("--tab", default=DEFAULT_TAB)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--final-format", action="append", default=None, dest="final_format")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(arguments)
    if args.interactive or args.sql_file is None:
        return None
    final_formats = tuple(args.final_format) if args.final_format else DEFAULT_FINAL_FORMATS
    return _build_config(args.base_url, args.database_id, args.schema, args.sql_file, args.page_size, args.query_limit, args.sql_editor_id, args.tab, args.workers, artifacts_dir=args.artifacts_dir, final_formats=final_formats, resume_run=args.resume_run)


def collect_config(input_value: Callable[[str], str] = input) -> Config:
    print("SQL Lab Extractor — tekan Enter untuk memakai nilai default.")
    base_url = _prompt(input_value, "Base URL", DEFAULT_BASE_URL)
    database_id = int(_prompt(input_value, "Database ID", str(DEFAULT_DATABASE_ID)))
    schema = _prompt(input_value, "Schema", DEFAULT_SCHEMA)
    sql_file = Path(_prompt(input_value, "File SQL", "query.sql"))
    page_size = int(_prompt(input_value, "Page size", str(DEFAULT_PAGE_SIZE)))
    query_limit = int(_prompt(input_value, "Query limit", str(DEFAULT_QUERY_LIMIT)))
    sql_editor_id = _prompt(input_value, "SQL editor ID", DEFAULT_SQL_EDITOR_ID)
    tab = _prompt(input_value, "Nama tab", DEFAULT_TAB)
    workers = int(_prompt(input_value, "Jumlah worker", str(DEFAULT_WORKERS)))
    artifacts_dir = Path(_prompt(input_value, "Direktori artifacts", str(DEFAULT_ARTIFACTS_DIR)))
    raw_formats = _prompt(input_value, "Format akhir (parquet/xlsx, pisahkan koma)", ",".join(DEFAULT_FINAL_FORMATS)).lower()
    final_formats = tuple(part.strip() for part in raw_formats.split(",") if part.strip())
    return _build_config(base_url, database_id, schema, sql_file, page_size, query_limit, sql_editor_id, tab, workers, artifacts_dir=artifacts_dir, final_formats=final_formats)


def _prompt(input_value: Callable[[str], str], label: str, default: str) -> str:
    return input_value(f"{label} [{default}]: ").strip() or default


def _build_config(base_url: str, database_id: int, schema: str, sql_file: Path, page_size: int, query_limit: int, sql_editor_id: str, tab: str, workers: int, artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR, final_formats: tuple[str, ...] = DEFAULT_FINAL_FORMATS, resume_run: Path | None = None) -> Config:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Base URL wajib HTTPS")
    if database_id <= 0 or page_size <= 0 or query_limit <= 0 or workers <= 0:
        raise ValueError("Database ID, page size, query limit, dan worker wajib positif")
    if workers > MAX_WORKERS:
        raise ValueError(f"Jumlah worker maksimal {MAX_WORKERS}")
    if page_size > MAX_PAGE_SIZE:
        raise ValueError(f"Page size maksimal {MAX_PAGE_SIZE}")
    if not final_formats or any(fmt not in FINAL_FORMAT_CHOICES for fmt in final_formats):
        raise ValueError("Format akhir wajib parquet dan/atau xlsx")
    return Config(base_url.rstrip("/"), database_id, schema, sql_file, page_size, query_limit, sql_editor_id, tab, workers, artifacts_dir, final_formats, resume_run)
