import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from sql_lab_extractor.artifacts import RunStore
from sql_lab_extractor.client import HttpClient, HttpStatusError, MAX_ERROR_BODY_BYTES


class HttpDiagnosticTests(unittest.TestCase):
    def test_persists_useful_bounded_redacted_error_body(self):
        body = json.dumps({
            "message": "Validation failed",
            "detail": "Missing required database field",
            "csrf_token": "csrf-secret",
            "authorization": "Bearer secret-token",
            "sql": "SELECT private FROM people",
            "data": [{"name": "private-person"}],
        }).encode()
        error = HTTPError(
            "https://app.example/api/v1/sqllab/execute/?token=secret#fragment",
            422,
            "Unprocessable Entity",
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
            io.BytesIO(body),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HttpStatusError) as caught:
                HttpClient("https://app.example", "csrf", "session=secret").request(
                    "POST", "/api/v1/sqllab/execute/?token=secret", {"sql": "SELECT secret"}
                )
        diagnostic = caught.exception.diagnostic
        rendered = json.dumps(diagnostic)
        self.assertEqual(diagnostic["status_code"], 422)
        self.assertEqual(diagnostic["method"], "POST")
        self.assertEqual(diagnostic["path"], "/api/v1/sqllab/execute/")
        self.assertEqual(diagnostic["body"]["message"], "Validation failed")
        self.assertIn("database field", diagnostic["body"]["detail"])
        for secret in ("csrf-secret", "secret-token", "SELECT private", "private-person", "token=secret"):
            self.assertNotIn(secret, rendered)

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore.create(Path(directory), "SELECT 1")
            store.record_failure(0, 1, caught.exception)
            persisted = (store.run_dir / "failures" / "offset-0.json").read_text(encoding="utf-8")
            self.assertIn("Validation failed", persisted)
            self.assertNotIn("csrf-secret", persisted)

    def test_truncates_large_text_error_body(self):
        body = ("ordinary diagnostic " * (MAX_ERROR_BODY_BYTES // 10)).encode()
        error = HTTPError(
            "https://app.example/api/fail",
            500,
            "Server Error",
            {"Content-Type": "text/plain"},
            io.BytesIO(body),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HttpStatusError) as caught:
                HttpClient("https://app.example", "csrf", "session=secret").request("GET", "/api/fail")
        self.assertTrue(caught.exception.diagnostic["body_truncated"])
        self.assertLessEqual(len(caught.exception.diagnostic["body"]), MAX_ERROR_BODY_BYTES)


if __name__ == "__main__":
    unittest.main()
