from __future__ import annotations

import json
import logging
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import unittest.mock

from sql_lab_extractor.artifacts import PageRecord
from sql_lab_extractor.auth import BrowserSession
from types import SimpleNamespace
from sql_lab_extractor.artifacts import RunStore
from sql_lab_extractor.query import QueryError


def make_config(root: Path, sql_file: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "artifacts_dir": root,
        "base_url": "https://example.test",
        "database_id": 25,
        "schema": "analytics",
        "sql_file": sql_file,
        "page_size": 1000,
        "query_limit": 100000,
        "sql_editor_id": "editor",
        "tab": "tab",
        "workers": 1,
        "final_formats": ("parquet",),
        "resume_run": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ResumeStatusBehaviorTests(unittest.TestCase):
    def test_resume_schedules_only_invalid_page_offsets(self):
        from sql_lab_extractor.__main__ import extract_run

        config = make_config(Path("artifacts"), Path("query.sql"))
        store = Mock()
        first_record = PageRecord(0, 1000, ("assignment_id",), "a" * 64)
        missing_record = PageRecord(1000, 1, ("assignment_id",), "b" * 64)
        store.validate_page_record.side_effect = [first_record, None, first_record, missing_record]
        store._read_manifest.return_value = {"invariants": {}}
        store.write_page.return_value = missing_record
        count_state = SimpleNamespace(data=[{"total_rows": 1001}])
        with (
            patch("sql_lab_extractor.__main__._execute_with_refresh", return_value=count_state),
            patch("sql_lab_extractor.__main__._fetch_sync_page", return_value=[{"assignment_id": "2"}]) as fetch,
        ):
            records = extract_run(config, "SELECT assignment_id FROM sample ORDER BY assignment_id", Mock(), client_factory=Mock(), run_store=store)

        self.assertEqual(records, [first_record, missing_record])
        self.assertEqual(store.validate_page_record.call_args_list, [unittest.mock.call(0), unittest.mock.call(1000), unittest.mock.call(0), unittest.mock.call(1000)])
        fetch.assert_called_once_with(1000, config, "SELECT assignment_id FROM sample ORDER BY assignment_id", unittest.mock.ANY, unittest.mock.ANY)

    def test_resume_rejects_invariant_drift_before_page_reuse(self):
        from sql_lab_extractor.__main__ import extract_run

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql_file = root / "query.sql"
            sql = "SELECT assignment_id FROM sample ORDER BY assignment_id"
            sql_file.write_text(sql, encoding="utf-8")
            store = RunStore.create(root, sql)
            store.record_invariant("query_sha256", "a" * 64)
            store.record_invariant("total_rows", 3)
            config = make_config(root, sql_file, resume_run=store.run_dir)
            count_state = SimpleNamespace(data=[{"total_rows": 4}])
            with patch("sql_lab_extractor.__main__._execute_with_refresh", return_value=count_state):
                with self.assertRaisesRegex(ValueError, "Invariant"):
                    extract_run(config, sql, Mock(), client_factory=Mock(), run_store=store)

    def test_main_marks_failed_when_extraction_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql_file = root / "query.sql"
            sql_file.write_text("SELECT assignment_id FROM sample ORDER BY assignment_id", encoding="utf-8")
            store = Mock(run_dir=root / "run-test")
            with (
                patch("sql_lab_extractor.__main__._load_session", return_value=BrowserSession("cookie", "csrf")),
                patch("sql_lab_extractor.__main__.RunStore.create", return_value=store),
                patch("sql_lab_extractor.__main__.extract_run", side_effect=QueryError("bad query")),
                patch("sys.stderr", new_callable=StringIO),
            ):
                from sql_lab_extractor.__main__ import main
                exit_code = main(["--sql-file", str(sql_file), "--artifacts-dir", str(root)])

            self.assertEqual(exit_code, 1)
            self.assertEqual(store.set_status.call_args_list, [(("running",),), (("failed",),)])

    def test_main_reports_safe_value_error_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql_file = root / "query.sql"
            sql_file.write_text("SELECT assignment_id FROM sample ORDER BY assignment_id", encoding="utf-8")
            store = Mock(run_dir=root / "run-test")
            stderr = StringIO()
            with (
                patch("sql_lab_extractor.__main__._load_session", return_value=BrowserSession("cookie", "csrf")),
                patch("sql_lab_extractor.__main__.RunStore.create", return_value=store),
                patch("sql_lab_extractor.__main__.extract_run", side_effect=ValueError("Invariant 'total_rows' tidak cocok: lama=3 baru=4")),
                patch("sys.stderr", stderr),
            ):
                from sql_lab_extractor.__main__ import main
                exit_code = main(["--sql-file", str(sql_file), "--artifacts-dir", str(root)])

            payload = json.loads(stderr.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["message"], "Invariant 'total_rows' tidak cocok: lama=3 baru=4")

    def test_main_marks_excel_finalization_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql_file = root / "query.sql"
            sql_file.write_text("SELECT assignment_id FROM sample ORDER BY assignment_id", encoding="utf-8")
            store = Mock(run_dir=root / "run-test")
            result = FinalizeResult("completed_with_excel_error", root / "result.parquet", None, 1, ("assignment_id",))
            with (
                patch("sql_lab_extractor.__main__._load_session", return_value=BrowserSession("cookie", "csrf")),
                patch("sql_lab_extractor.__main__.RunStore.create", return_value=store),
                patch("sql_lab_extractor.__main__.extract_run", return_value=[]),
                patch("sql_lab_extractor.__main__.finalize_run", return_value=result),
                redirect_stdout(StringIO()),
            ):
                from sql_lab_extractor.__main__ import main
                exit_code = main(["--sql-file", str(sql_file), "--artifacts-dir", str(root)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(store.set_status.call_args_list, [(("running",),), (("completed_with_excel_error",),)])
from sql_lab_extractor.finalize import FinalizeResult



class RunLogTests(unittest.TestCase):
    def test_run_log_redacts_sql_secrets_and_rows(self):
        from sql_lab_extractor.__main__ import _attach_run_log

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            handler = _attach_run_log(run_dir)
            try:
                logging.getLogger("sql_lab_extractor.test").warning(
                    "sql=SELECT secret cookie=abc rows=[{'private': 1}]"
                )
            finally:
                logging.getLogger().removeHandler(handler)
                handler.close()

            content = (run_dir / "run.log").read_text(encoding="utf-8")
            self.assertNotIn("SELECT", content)
            self.assertNotIn("abc", content)
            self.assertNotIn("private", content)

class SharedSessionTests(unittest.TestCase):
    def test_main_uses_artifacts_session_profile_and_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql_file = root / "query.sql"
            sql_file.write_text("SELECT name FROM sample ORDER BY name", encoding="utf-8")
            store = Mock(run_dir=root / "run-test")
            result = FinalizeResult("completed", root / "result.parquet", None, 0, ())
            with (
                patch("sql_lab_extractor.__main__.RunStore.create", return_value=store),
                patch("sql_lab_extractor.__main__.extract_run", return_value=[]) as extract,
                patch("sql_lab_extractor.__main__.finalize_run", return_value=result),
            ):
                from sql_lab_extractor.__main__ import main
                self.assertEqual(main(["--sql-file", str(sql_file), "--artifacts-dir", str(root)]), 0)

            coordinator = extract.call_args.args[2]
            self.assertEqual(coordinator.profile_dir, root / "session" / "profile")
            self.assertEqual(coordinator.lock_path, root / "session" / "session.lock")


class CliIntegrationTests(unittest.TestCase):
    def test_main_uses_run_store_extraction_and_finalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql_file = root / "query.sql"
            sql_file.write_text("SELECT assignment_id FROM sample ORDER BY assignment_id", encoding="utf-8")
            store = Mock()
            store.run_dir = root / "run-test"
            result = FinalizeResult(
                "completed",
                store.run_dir / "result.parquet",
                store.run_dir / "result.xlsx",
                1,
                ("assignment_id",),
            )
            output = StringIO()
            with (
                patch("sql_lab_extractor.__main__._load_session", return_value=BrowserSession("cookie", "csrf")),
                patch("sql_lab_extractor.__main__.RunStore.create", return_value=store) as create_store,
                patch("sql_lab_extractor.__main__.extract_run", return_value=[PageRecord(0, 1, ("assignment_id",), "a" * 64)]) as extract,
                patch("sql_lab_extractor.__main__.finalize_run", return_value=result) as finalize,
                redirect_stdout(output),
            ):
                from sql_lab_extractor.__main__ import main

                exit_code = main(["--sql-file", str(sql_file), "--artifacts-dir", str(root), "--final-format", "parquet"])

            self.assertEqual(exit_code, 0)
            create_store.assert_called_once_with(root, sql_file.read_text(encoding="utf-8"))
            extract.assert_called_once()
            payload = json.loads(output.getvalue())
            finalize.assert_called_once_with(store, extract.return_value, ("parquet",))
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run_dir"], str(store.run_dir))
            self.assertNotIn("sql", payload)


if __name__ == "__main__":
    unittest.main()
