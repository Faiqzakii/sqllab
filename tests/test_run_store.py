import json
import re
import csv
import hashlib
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sql_lab_extractor.artifacts import PageRecord, RunStore
from sql_lab_extractor.client import HttpStatusError


class RunStoreTests(unittest.TestCase):
    def test_run_directories_have_unique_names(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = RunStore.create(root, "SELECT 1")
            second = RunStore.create(root, "SELECT 1")
            self.assertNotEqual(first.run_dir, second.run_dir)
            self.assertEqual(first.run_dir.parent, root)
            self.assertTrue(re.fullmatch(r"run-\d{8}T\d{6}Z-[0-9a-f]{8}", first.run_dir.name))
            self.assertTrue(first.run_dir.is_dir())
            self.assertTrue((first.run_dir / "pages").is_dir())
            self.assertTrue((first.run_dir / "failures").is_dir())

    def test_query_file_stores_exact_sql(self):
        sql = "SELECT a, b FROM t WHERE note = 'x; y';\n-- trailing comment\n"
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), sql)
            self.assertEqual((store.run_dir / "query.sql").read_text(encoding="utf-8"), sql)
            self.assertNotIn(sql, (store.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_records_creation_and_page_updates_atomically(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            manifest = json.loads((store.run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "created")
            self.assertIn("created_at", manifest)
            self.assertNotIn("sql", manifest)
            record = store.write_page(0, [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}])
            manifest = json.loads((store.run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["pages"]["0"]["checksum"], record.checksum)
            self.assertEqual(manifest["pages"]["0"]["rows"], 2)
            self.assertEqual(manifest["pages"]["0"]["columns"], ["id", "name"])

    def test_manifest_status_and_invariants_update_atomically(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            store.record_invariant("page_size", 1000)
            store.set_status("running")

            manifest = json.loads((store.run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "running")
            self.assertEqual(manifest["invariants"], {"page_size": 1000})

    def test_records_non_secret_resume_configuration(self):
        from types import SimpleNamespace

        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            store.record_config(SimpleNamespace(
                base_url="https://example.test", database_id=9, schema="sample", page_size=500,
                query_limit=50000, sql_editor_id="77", tab="Stored", workers=3,
                final_formats=("parquet",),
            ))
            manifest = json.loads((store.run_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["config"]["workers"], 3)
        self.assertNotIn("cookie", manifest["config"])
        self.assertNotIn("csrf", manifest["config"])

    def test_validate_page_record_checks_page_without_loading_rows(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            record = store.write_page(0, [{"id": "1", "name": "alpha"}])

            self.assertEqual(store.validate_page_record(0), record)
            self.assertIsNone(store.validate_page_record(1000))

            page = store.run_dir / "pages" / "offset-0.parquet"
            page.write_bytes(page.read_bytes() + b"corrupt")
            self.assertIsNone(store.validate_page_record(0))

    def test_concurrent_write_page_manifest_entries_all_survive(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            offsets = list(range(60))
            threads = [
                threading.Thread(target=store.write_page, args=(offset, [{"id": offset, "name": "v"}]))
                for offset in offsets
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            manifest = json.loads((store.run_dir / "manifest.json").read_text(encoding="utf-8"))
            entries = manifest.get("pages", {})
            self.assertEqual(
                set(entries),
                {str(offset) for offset in offsets},
                "every page entry must survive concurrent read-modify-write",
            )
            for offset in offsets:
                entry = entries[str(offset)]
                self.assertEqual(entry["rows"], 1)
                self.assertEqual(entry["columns"], ["id", "name"])
                self.assertRegex(entry["checksum"], r"^[0-9a-f]{64}$")

    def test_progress_events_are_valid_json_objects_per_line(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            store.append_event({"event": "run_started", "offsets": [0, 1000]})
            store.append_event({"event": "page_completed", "offset": 0, "rows": 2})
            lines = (store.run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            events = [json.loads(line) for line in lines]
            self.assertEqual(events[0]["event"], "run_started")
            self.assertEqual(events[1]["offset"], 0)

    def test_concurrent_progress_appends_never_interleave(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            threads = [
                threading.Thread(target=lambda index=index: [store.append_event({"event": "tick", "worker": index, "tick": tick}) for tick in range(25)])
                for index in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            lines = (store.run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 100)
            for line in lines:
                event = json.loads(line)
                self.assertEqual(event["event"], "tick")

    def test_write_page_creates_atomic_parquet_and_returns_record(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore.create(root, "SELECT 1")
            rows = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
            record = store.write_page(0, rows)
            self.assertIsInstance(record, PageRecord)
            self.assertEqual(record.offset, 0)
            self.assertEqual(record.rows, 2)
            self.assertEqual(record.columns, ("id", "name"))
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", record.checksum))
            page = store.run_dir / "pages" / "offset-0.parquet"
            self.assertTrue(page.is_file())
            self.assertEqual(list(root.rglob("*.partial")), [])

    def test_write_page_rejects_schema_drift_within_page(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore.create(root, "SELECT 1")
            with self.assertRaises(ValueError):
                store.write_page(0, [{"id": 1}, {"id": 2, "extra": "drift"}])
            self.assertFalse((store.run_dir / "pages" / "offset-0.parquet").exists())
            self.assertEqual(list(root.rglob("*.partial")), [])

    def test_load_valid_page_round_trips_rows(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            rows = [{"id": "1", "name": "alpha"}, {"id": "2", "name": "beta"}]
            store.write_page(1000, rows)
            self.assertEqual(store.load_valid_page(1000), rows)

    def test_load_valid_page_returns_none_for_missing_or_invalid_pages(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore.create(root, "SELECT 1")
            self.assertIsNone(store.load_valid_page(500))
            store.write_page(0, [{"id": "1", "name": "alpha"}])
            page = store.run_dir / "pages" / "offset-0.parquet"
            original = page.read_bytes()
            page.write_bytes(original + b"corrupt")
            self.assertIsNone(store.load_valid_page(0))
            page.write_bytes(original)
            self.assertIsNotNone(store.load_valid_page(0))
            page.write_bytes(b"not parquet")
            self.assertIsNone(store.load_valid_page(0))

    def test_loads_legacy_csv_page_for_resume(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            page = store.run_dir / "pages" / "offset-0.csv"
            with page.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=("id", "name"))
                writer.writeheader()
                writer.writerow({"id": "1", "name": "a,b"})
            checksum = hashlib.sha256(page.read_bytes()).hexdigest()
            store._record_page_in_manifest(PageRecord(0, 1, ("id", "name"), checksum))

            self.assertEqual(store.load_valid_page(0), [{"id": "1", "name": "a,b"}])

    def test_record_failure_stores_only_type_category_and_fixed_message(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            error = {
                "message": "HTTP 401 cookie=session=topsecretvalue123",
                "cookie": "session=topsecretvalue123",
                "csrf": "csrf-token-abc",
                "sql": "SELECT password FROM users",
                "rows": [{"password": "hunter2"}],
                "body": '{"error": "Bearer super-secret-token-xyz rejected"}',
                "nested": {"note": "token topsecretvalue123 seen"},
            }
            store.record_failure(2000, 3, error)
            raw = (store.run_dir / "failures" / "offset-2000.json").read_text(encoding="utf-8")
            for secret in (
                "topsecretvalue123",
                "csrf-token-abc",
                "SELECT password",
                "hunter2",
                "super-secret-token-xyz",
                "Bearer",
                "cookie",
                "csrf",
                "body",
            ):
                self.assertNotIn(secret, raw)
            payload = json.loads(raw)
            self.assertEqual(payload["offset"], 2000)
            self.assertEqual(payload["attempt"], 3)
            self.assertIn("recorded_at", payload)
            # Only an error type/category and a fixed safe message are persisted,
            # never raw free-text from the response body or exception message.
            self.assertEqual(payload["error"]["type"], "dict")
            self.assertEqual(payload["error"]["message"], "error recorded")
            self.assertNotIn("cookie", payload["error"])
            self.assertNotIn("csrf", payload["error"])
            self.assertNotIn("body", payload["error"])
            self.assertNotIn("rows", payload["error"])
            lines = (store.run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
            failed_event = json.loads(lines[-1])
            self.assertEqual(failed_event["event"], "page_failed")
            self.assertEqual(failed_event["error_type"], "dict")
            self.assertEqual(failed_event["error"], "error recorded")

    def test_record_failure_stores_http_status_without_raw_error_text(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            error = HttpStatusError(503)
            error.args = ("HTTP 503 body=topsecretvalue123 cookie=session-secret",)

            store.record_failure(3000, 2, error)

            failure_raw = (store.run_dir / "failures" / "offset-3000.json").read_text(
                encoding="utf-8"
            )
            progress_raw = (store.run_dir / "progress.jsonl").read_text(encoding="utf-8")
            for secret in ("topsecretvalue123", "session-secret", "body", "cookie"):
                self.assertNotIn(secret, failure_raw)
                self.assertNotIn(secret, progress_raw)
            payload = json.loads(failure_raw)
            self.assertEqual(
                payload["error"],
                {
                    "type": "HttpStatusError",
                    "category": "failure",
                    "message": "error recorded",
                    "status_code": 503,
                },
            )

    def test_record_failure_redacts_raw_unkeyed_secret_strings(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            # A raw bearer token embedded in free text without a key=value form.
            error = RuntimeError("Authorization Bearer super-secret-token-xyz was rejected")
            store.record_failure(4000, 1, error)
            raw = (store.run_dir / "failures" / "offset-4000.json").read_text(encoding="utf-8")
            for secret in ("super-secret-token-xyz", "Bearer", "Authorization"):
                self.assertNotIn(secret, raw)
            payload = json.loads(raw)
            self.assertEqual(payload["error"]["type"], "RuntimeError")
            self.assertEqual(payload["error"]["message"], "error recorded")

    def test_record_failure_redacts_multi_word_secret_strings(self):
        with TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), "SELECT 1")
            # A multi-word secret phrase that the old key=value regex would miss.
            error = ValueError("failed because my super secret token abc def leaked")
            store.record_failure(5000, 2, error)
            raw = (store.run_dir / "failures" / "offset-5000.json").read_text(encoding="utf-8")
            for secret in ("my super secret token abc def", "super secret token", "abc def", "leaked"):
                self.assertNotIn(secret, raw)
            payload = json.loads(raw)
            self.assertEqual(payload["error"]["type"], "ValueError")
            self.assertEqual(payload["error"]["message"], "error recorded")


if __name__ == "__main__":
    unittest.main()
