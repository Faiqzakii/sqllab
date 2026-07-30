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

    def test_sensitive_local_artifacts_are_ignored(self):
        ignore_file = Path(".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", "sql_lab_browser.log", "*.har", "*.partial", "artifacts/", "*.log"):
            self.assertIn(pattern, ignore_file)
        # The persistent shared profile under artifacts/session/profile/ must be
        # explicitly ignored so browser state is never committed.
        self.assertIn("artifacts/session/profile/", ignore_file)


if __name__ == "__main__":
    unittest.main()
