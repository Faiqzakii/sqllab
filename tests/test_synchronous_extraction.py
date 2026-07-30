from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from sql_lab_extractor.__main__ import extract_run
from sql_lab_extractor.auth import BrowserSession
from sql_lab_extractor.client import HttpStatusError
from sql_lab_extractor.config import parse_args
from sql_lab_extractor.session import SessionCoordinator


class SynchronousExtractionTests(unittest.TestCase):
    def test_writes_completed_pages_and_returns_records_by_offset(self):
        class Client:
            def request(self, method, path, payload=None):
                sql = payload["sql"]
                if "COUNT(1)" in sql:
                    return {"status": "success", "data": [{"total_rows": 5}]}
                offset = int(re.search(r"OFFSET (\d+)$", sql).group(1))
                return {"status": "success", "data": [{"assignment_id": str(offset)}]}

        with tempfile.TemporaryDirectory() as directory:
            config = parse_args([
                "--sql-file", "q.sql", "--page-size", "2", "--workers", "2",
                "--artifacts-dir", directory,
            ])
            coordinator = SessionCoordinator(refresh=lambda: BrowserSession("cookie", "csrf"))
            records = extract_run(
                config,
                "SELECT assignment_id FROM sample ORDER BY assignment_id",
                coordinator,
                client_factory=lambda snapshot: Client(),
            )
            self.assertEqual([record.offset for record in records], [0, 2, 4])
            run_dir = next(Path(directory).iterdir())
            events = [json.loads(line) for line in (run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["event"] for event in events].count("page_completed"), 3)

    def test_starts_pages_before_execution_with_at_most_worker_count_in_flight(self):
        active = 0
        highest_active = 0

        class Client:
            def request(self, method, path, payload=None):
                nonlocal active, highest_active
                sql = payload["sql"]
                if "COUNT(1)" in sql:
                    return {"status": "success", "data": [{"total_rows": 5}]}
                active += 1
                highest_active = max(highest_active, active)
                try:
                    time.sleep(0.01)
                    offset = int(re.search(r"OFFSET (\d+)$", sql).group(1))
                    return {"status": "success", "data": [{"assignment_id": str(offset)}]}
                finally:
                    active -= 1

        with tempfile.TemporaryDirectory() as directory:
            config = parse_args(["--sql-file", "q.sql", "--page-size", "1", "--workers", "2", "--artifacts-dir", directory])
            coordinator = SessionCoordinator(refresh=lambda: BrowserSession("cookie", "csrf"))
            extract_run(config, "SELECT assignment_id FROM sample ORDER BY assignment_id", coordinator, client_factory=lambda snapshot: Client())
            run_dir = next(Path(directory).iterdir())
            events = [json.loads(line) for line in (run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertLessEqual(highest_active, 2)
        for offset in range(0, 5):
            started = next(index for index, event in enumerate(events) if event["event"] == "page_started" and event["offset"] == offset)
            completed = next(index for index, event in enumerate(events) if event["event"] == "page_completed" and event["offset"] == offset)
            self.assertLess(started, completed)

    def test_page_start_limiter_spaces_execute_posts(self):
        from sql_lab_extractor.__main__ import PAGE_START_INTERVAL_SECONDS, _wait_for_page_slot

        sleeps = []
        clock_values = iter([10.0, 10.5])
        started_at = _wait_for_page_slot(9.0, clock=lambda: next(clock_values), sleep=sleeps.append)

        self.assertEqual(sleeps, [PAGE_START_INTERVAL_SECONDS - 1.0])
        self.assertEqual(started_at, 10.5)

    def test_fetches_terminal_results_key_once_without_reposting(self):
        class Client:
            def __init__(self):
                self.calls = []

            def request(self, method, path, payload=None):
                self.calls.append((method, path, payload))
                if method == "POST":
                    return {"status": "success", "query": {"id": "query-7"}, "resultsKey": "key-7"}
                if path != "/api/v1/sqllab/results/?q=(key:key-7,rows:1000)":
                    raise AssertionError(path)
                return {"data": [{"id": 7}]}

        from sql_lab_extractor.query import execute_sync
        client = Client()
        state = execute_sync(client, {"sql": "SELECT 7"})

        self.assertEqual(state.query_id, "query-7")
        self.assertEqual(state.results_key, "key-7")
        self.assertEqual(state.data, [{"id": 7}])
        self.assertEqual([call[:2] for call in client.calls], [
            ("POST", "/api/v1/sqllab/execute/"),
            ("GET", "/api/v1/sqllab/results/?q=(key:key-7,rows:1000)"),
        ])

    def test_does_not_replay_execute_after_authenticated_401(self):
        calls = []

        class Client:
            def __init__(self, generation):
                self.generation = generation

            def request(self, method, path, payload=None):
                sql = payload["sql"]
                calls.append((self.generation, sql))
                if "COUNT(1)" in sql:
                    return {"status": "success", "data": [{"total_rows": 1}]}
                raise HttpStatusError(401)

        with tempfile.TemporaryDirectory() as directory:
            config = parse_args(["--sql-file", "q.sql", "--artifacts-dir", directory])
            coordinator = SessionCoordinator(refresh=lambda: BrowserSession("cookie", "csrf"))
            with self.assertRaises(HttpStatusError):
                extract_run(
                    config,
                    "SELECT assignment_id FROM sample ORDER BY assignment_id",
                    coordinator,
                    client_factory=lambda snapshot: Client(snapshot.generation),
                )
        self.assertEqual([generation for generation, sql in calls if "OFFSET" in sql], [1])
        self.assertEqual(coordinator.get_snapshot().generation, 1)

    def test_does_not_retry_ambiguous_execute_timeout(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def request(self, method, path, payload=None):
                self.calls += 1
                if "COUNT(1)" in payload["sql"]:
                    return {"status": "success", "data": [{"total_rows": 1}]}
                raise RuntimeError("HTTP request timed out; execute was not retried")

        with tempfile.TemporaryDirectory() as directory:
            config = parse_args(["--sql-file", "q.sql", "--artifacts-dir", directory])
            coordinator = SessionCoordinator(refresh=lambda: BrowserSession("cookie", "csrf"))
            client = Client()
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                extract_run(
                    config,
                    "SELECT assignment_id FROM sample ORDER BY assignment_id",
                    coordinator,
                    client_factory=lambda snapshot: client,
                )
        self.assertEqual(client.calls, 2)


if __name__ == "__main__":
    unittest.main()
