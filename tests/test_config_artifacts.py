import unittest
from pathlib import Path

from sql_lab_extractor.config import collect_config, parse_args


class ConfigArtifactsTests(unittest.TestCase):
    def test_defaults_add_artifacts_dir_and_final_formats(self):
        config = parse_args(["--sql-file", "q.sql"])
        self.assertEqual(config.artifacts_dir, Path("artifacts"))
        self.assertEqual(config.final_formats, ("parquet", "xlsx"))

    def test_artifacts_dir_flag_is_accepted(self):
        config = parse_args(["--sql-file", "q.sql", "--artifacts-dir", "out/runs"])
        self.assertEqual(config.artifacts_dir, Path("out/runs"))

    def test_explicit_final_format_selection_is_accepted(self):
        single = parse_args(["--sql-file", "q.sql", "--final-format", "parquet"])
        self.assertEqual(single.final_formats, ("parquet",))
        repeated = parse_args(["--sql-file", "q.sql", "--final-format", "parquet", "--final-format", "xlsx"])
        self.assertEqual(repeated.final_formats, ("parquet", "xlsx"))

    def test_invalid_final_format_fails(self):
        with self.assertRaises(ValueError):
            parse_args(["--sql-file", "q.sql", "--final-format", "csv"])
        with self.assertRaises(ValueError):
            collect_config(input_value=lambda prompt: "csv" if "Format akhir" in prompt else "")

    def test_interactive_defaults_include_artifacts_and_final_formats(self):
        config = collect_config(input_value=lambda prompt: "")
        self.assertEqual(config.artifacts_dir, Path("artifacts"))
        self.assertEqual(config.final_formats, ("parquet", "xlsx"))
        self.assertIsNone(config.resume_run)

    def test_rejects_workers_above_safe_maximum(self):
        with self.assertRaisesRegex(ValueError, "maksimal"):
            parse_args(["--sql-file", "q.sql", "--workers", "9"])

    def test_rejects_page_size_above_safe_maximum(self):
        with self.assertRaisesRegex(ValueError, "Page size maksimal"):
            parse_args(["--sql-file", "q.sql", "--page-size", "10001"])

    def test_resume_loads_stored_query_and_config_with_worker_override(self):
        import json
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-example"
            run_dir.mkdir()
            (run_dir / "query.sql").write_text("SELECT 1 ORDER BY 1", encoding="utf-8")
            (run_dir / "manifest.json").write_text(json.dumps({"config": {
                "base_url": "https://example.test",
                "database_id": 9,
                "schema": "stored_schema",
                "page_size": 500,
                "query_limit": 50000,
                "sql_editor_id": "77",
                "tab": "Stored",
                "workers": 4,
                "final_formats": ["parquet"],
            }}), encoding="utf-8")

            config = parse_args(["--resume-run", str(run_dir), "--workers", "2"])

        self.assertEqual(config.sql_file, run_dir / "query.sql")
        self.assertEqual(config.page_size, 500)
        self.assertEqual(config.workers, 2)
        self.assertEqual(config.final_formats, ("parquet",))

    def test_legacy_resume_without_stored_config_requires_sql_file(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-old"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "--sql-file"):
                parse_args(["--resume-run", str(run_dir)])

    def test_sensitive_local_artifacts_are_ignored(self):
        ignore_file = Path(".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", "sql_lab_browser.log", "*.har", "*.partial", "artifacts/", "*.log"):
            self.assertIn(pattern, ignore_file)
        # The persistent shared profile under artifacts/session/profile/ must be
        # explicitly ignored so browser state is never committed.
        self.assertIn("artifacts/session/profile/", ignore_file)


if __name__ == "__main__":
    unittest.main()
