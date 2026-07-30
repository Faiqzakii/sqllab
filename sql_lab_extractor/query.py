from __future__ import annotations

import re
import logging

import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
POLL_LOG_INTERVAL = 10


class QueryError(ValueError):
    pass


_SELECT_LIST_ERROR = "Kolom select tidak dapat diproses dengan aman"


def _select_list_bounds(sql: str) -> tuple[int, int]:
    depth = 0
    start: int | None = None
    tokens = re.finditer(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\(|\)|\bSELECT\b|\bFROM\b", sql, re.IGNORECASE
    )
    for match in tokens:
        token = match.group(0)
        if token.startswith(("'", '"', "`")):
            continue
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and token.upper() == "SELECT":
            if start is not None:
                raise QueryError(_SELECT_LIST_ERROR)
            start = match.end()
        elif depth == 0 and token.upper() == "FROM" and start is not None:
            return start, match.start()
    raise QueryError(_SELECT_LIST_ERROR)


def _split_top_level_commas(select_list: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    tokens = re.finditer(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\(|\)|,", select_list)
    for match in tokens:
        token = match.group(0)
        if token.startswith(("'", '"', "`")):
            continue
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif token == "," and depth == 0:
            parts.append(select_list[start:match.start()].strip())
            start = match.end()
    parts.append(select_list[start:].strip())
    if any(not part for part in parts):
        raise QueryError(_SELECT_LIST_ERROR)
    return parts


def _column_alias(item: str) -> str | None:
    match = re.search(
        r'(?is)\s+AS\s+(?:("[^"]*(?:""[^"]*)*")|(`[^`]*(?:``[^`]*)*`)|([A-Za-z_][A-Za-z0-9_]*))\s*$', item
    )
    if match is not None:
        alias = match.group(3) if match.group(3) is not None else (match.group(1) or match.group(2))
        return alias.strip('"`').replace('""', '"').replace("``", "`")
    if re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*", item):
        return item.split(".")[-1]
    return None


def build_column_chunks(sql: str, max_columns: int, key_column: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_column):
        raise QueryError("Kolom kunci tidak valid")
    if not isinstance(max_columns, int) or max_columns < 1:
        raise QueryError("Ukuran chunk tidak valid")
    normalized = re.sub(r"--[^\n]*|--[^\n]*$|/\*(?:[^*]|\*(?!/))*\*/", " ", sql, flags=re.MULTILINE | re.DOTALL)
    if "--" in normalized or "/*" in normalized or "*/" in normalized or normalized != sql:
        raise QueryError(_SELECT_LIST_ERROR)
    normalized = normalized.strip().removesuffix(";").strip()
    if not normalized:
        raise QueryError(_SELECT_LIST_ERROR)
    if ";" in normalized:
        raise QueryError(_SELECT_LIST_ERROR)
    if re.match(r"(?is)^\s*WITH\b", normalized):
        raise QueryError(_SELECT_LIST_ERROR)
    if re.search(r"(?is)\b(UNION|INTERSECT|EXCEPT)\b", normalized):
        raise QueryError(_SELECT_LIST_ERROR)
    start, end = _select_list_bounds(normalized)
    head, tail = normalized[:start].rstrip(), normalized[end:].strip()
    items = _split_top_level_commas(normalized[start:end])
    if any(re.search(r"(?s)\*", item) for item in items):
        raise QueryError(_SELECT_LIST_ERROR)
    distinct = re.match(r"(?is)^(DISTINCT)\s+(.+)$", items[0])
    if distinct:
        items[0] = distinct.group(2).strip()
        head = f"{head} {distinct.group(1)}"
    aliases = [_column_alias(item) for item in items]
    key_indexes = [index for index, alias in enumerate(aliases) if alias == key_column]
    if len(key_indexes) != 1 or len(set(aliases)) != len(aliases):
        raise QueryError("Kolom kunci wajib ada dan unik")
    key_item = items[key_indexes[0]]
    others = [item for index, item in enumerate(items) if index != key_indexes[0]]
    if not others:
        return [normalized]
    if max_columns < 2:
        raise QueryError("Ukuran chunk tidak valid")
    return [
        f"{head} {', '.join([key_item] + others[index : index + max_columns - 1])} {tail}"
        for index in range(0, len(others), max_columns - 1)
    ]


def merge_column_pages(chunks: list[list[dict[str, Any]]], key_column: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_column):
        raise QueryError("Kolom kunci tidak valid")
    if not chunks:
        raise QueryError("Tidak ada chunk untuk digabung")
    merged: dict[Any, dict[str, Any]] = {}
    expected_keys: set[Any] | None = None
    for chunk in chunks:
        seen: set[Any] = set()
        for row in chunk:
            if key_column not in row:
                raise QueryError("Kolom kunci tidak ditemukan pada baris")
            key = row[key_column]
            if key in seen:
                raise QueryError("Kolom kunci duplikat pada chunk")
            seen.add(key)
            target = merged.setdefault(key, {key_column: key})
            for column, value in row.items():
                if column == key_column:
                    continue
                if column in target and target[column] != value:
                    raise QueryError("Nilai kolom bertentangan antar chunk")
                target[column] = value
        if expected_keys is None:
            expected_keys = seen
        elif seen != expected_keys:
            raise QueryError("Kumpulan kunci berbeda antar chunk")
    return list(merged.values())


@dataclass(frozen=True)
class QueryState:
    state: str
    query_id: str | None = None
    results_key: str | None = None
    data: list[dict[str, Any]] | None = None


def has_top_level_order_by(sql: str) -> bool:
    depth = 0
    tokens = re.finditer(r"\(|\)|\bORDER\s+BY\b|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", sql, re.IGNORECASE)
    for match in tokens:
        token = match.group(0)
        if token.startswith(("'", '"')):
            continue
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            return True
    return False


def build_count_query(sql: str) -> str:
    base_sql = _strip_top_level_paging(sql)
    return f"SELECT COUNT(1) AS total_rows FROM ({base_sql}) AS sql_lab_count"


def _strip_top_level_paging(sql: str) -> str:
    normalized = sql.strip().removesuffix(";").strip()
    clauses = list(_top_level_clauses(normalized))
    cut = min((position for name, position in clauses if name in {"ORDER BY", "LIMIT"}), default=len(normalized))
    return normalized[:cut].rstrip()


def _top_level_clauses(sql: str):
    depth = 0
    tokens = re.finditer(r"\(|\)|\bORDER\s+BY\b|\bLIMIT\b|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", sql, re.IGNORECASE)
    for match in tokens:
        token = match.group(0)
        if token.startswith(("'", '"')):
            continue
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            yield re.sub(r"\s+", " ", token.upper()), match.start()


def build_offset_query(sql: str, limit: int, offset: int) -> str:
    if limit <= 0 or offset < 0:
        raise QueryError("Limit dan offset tidak valid")
    normalized = sql.strip().removesuffix(";").strip()
    if not has_top_level_order_by(normalized):
        raise QueryError("SQL wajib memiliki top-level ORDER BY")
    limit_positions = [position for name, position in _top_level_clauses(normalized) if name == "LIMIT"]
    if limit_positions:
        normalized = normalized[:limit_positions[0]].rstrip()
    return f"{normalized} LIMIT {limit} OFFSET {offset}"


def interpret_execute(payload: dict[str, Any]) -> QueryState:
    status = str(payload.get("status", "")).lower()
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    query_id = query.get("id") or payload.get("queryId")
    results_key = payload.get("resultsKey") or query.get("resultsKey")
    if status in {"success", "succeeded"}:
        data = payload.get("data")
        if data is not None and not isinstance(data, list):
            raise QueryError("Malformed query data")
        return QueryState("success", _text(query_id), _text(results_key), data)
    if status in {"pending", "submitted", "running"} and query_id is not None:
        return QueryState("running", str(query_id), _text(results_key))
    if status in {"failed", "error", "cancelled", "canceled", "timed_out", "timeout"}:
        return QueryState(status, _text(query_id), _text(results_key))
    raise QueryError("Malformed execute response")


def execute_sync(client: Any, payload: dict[str, Any], stage: str = "query") -> QueryState:
    """Execute once and make one correlated result fetch when required."""
    started = time.monotonic()
    logger.info("event=query_started stage=%s timeout_seconds=%s", stage, getattr(client, "timeout", "unknown"))
    try:
        state = interpret_execute(client.request("POST", "/api/v1/sqllab/execute/", payload))
        if state.state != "success":
            raise QueryError(f"Query selesai dengan status {state.state}")
        if state.data is None:
            if state.results_key is None:
                raise QueryError("Hasil synchronous tidak tersedia")
            result = client.request("GET", f"/api/v1/sqllab/results/?q=(key:{state.results_key},rows:1000)")
            data = result.get("data")
            if not isinstance(data, list):
                raise QueryError("Malformed result response")
            state = QueryState("success", state.query_id, state.results_key, data)
    except BaseException as error:
        logger.error("event=query_failed stage=%s elapsed_ms=%d error_type=%s", stage, int((time.monotonic() - started) * 1000), type(error).__name__)
        raise
    logger.info("event=query_completed stage=%s elapsed_ms=%d rows=%d", stage, int((time.monotonic() - started) * 1000), len(state.data or ()))
    return state


def execute_query(client: Any, payload: dict[str, Any], poll_interval: float = 2.0, max_polls: int = 150) -> QueryState:
    logger.info("Submitting SQL Lab query")
    state = interpret_execute(client.request("POST", "/api/v1/sqllab/execute/", payload))
    if state.state == "success":
        logger.info("SQL Lab query completed synchronously")
        return state
    if state.state != "running" or state.query_id is None:
        raise QueryError(f"Query selesai dengan status {state.state}")
    logger.info("Query polling started: query_id=%s", state.query_id)
    for poll_number in range(1, max_polls + 1):
        feed = client.request("GET", "/api/v1/query/updated_since")
        entries = feed.get("result", [])
        if not isinstance(entries, list):
            raise QueryError("Malformed polling response")
        match = next((entry for entry in entries if isinstance(entry, dict) and str(entry.get("id")) == state.query_id), None)
        if match is not None:
            polled = interpret_execute({"status": match.get("status"), "query": match})
            if polled.state == "success" and polled.results_key:
                logger.info("Query completed; fetching result: query_id=%s", state.query_id)
                result = client.request("GET", f"/api/v1/sqllab/results/?q=(key:{polled.results_key},rows:1000)")
                data = result.get("data")
                if not isinstance(data, list):
                    raise QueryError("Malformed result response")
                return QueryState("success", state.query_id, polled.results_key, data)
            if polled.state != "running":
                raise QueryError(f"Query selesai dengan status {polled.state}")
        if poll_number == 1 or poll_number % POLL_LOG_INTERVAL == 0:
            logger.info("Query still running: query_id=%s poll=%d/%d", state.query_id, poll_number, max_polls)
        time.sleep(poll_interval)
    raise QueryError("Query polling timed out")

def _text(value: Any) -> str | None:
    return None if value is None else str(value)
