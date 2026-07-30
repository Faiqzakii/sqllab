import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sql_lab_extractor.artifacts import RunStore
from sql_lab_extractor.client import HttpClient, HttpResponseError, HttpTimeoutError


class CountDiagnosticTests(unittest.TestCase):
    def test_malformed_success_response_keeps_sanitized_body_diagnostic(self):
        class Response(io.BytesIO):
            headers = {"Content-Type": "text/html", "Content-Length": "78"}
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        body = b'<html><p>Gateway rejected request token=secret-token</p></html>'
        with patch("urllib.request.urlopen", return_value=Response(body)):
            with self.assertRaises(HttpResponseError) as caught:
                HttpClient("https://app.example", "csrf", "session=secret").request(
                    "POST", "/api/v1/sqllab/execute/", {"sql": "SELECT private"}
                )
        rendered = json.dumps(caught.exception.diagnostic)
        self.assertIn("Gateway rejected request", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertEqual(caught.exception.diagnostic["status_code"], 200)

    def test_success_response_larger_than_diagnostic_limit_is_not_truncated(self):
        rows = [{"assignment_id": str(index), "name": "x" * 100} for index in range(1000)]
        encoded = json.dumps({"status": "success", "data": rows}).encode("utf-8")

        class Response(io.BytesIO):
            headers = {"Content-Type": "application/json"}
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        self.assertGreater(len(encoded), 64 * 1024)
        with patch("urllib.request.urlopen", return_value=Response(encoded)):
            result = HttpClient("https://app.example", "csrf", "session=value").request("POST", "/execute")
        self.assertEqual(len(result["data"]), 1000)

    def test_timeout_uses_bounded_query_timeout_and_persists_diagnostic(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            with self.assertRaises(HttpTimeoutError) as caught:
                HttpClient("https://app.example", "csrf", "session=secret").request(
                    "POST", "/api/v1/sqllab/execute/?token=secret", {"sql": "SELECT private"}
                )
        self.assertEqual(caught.exception.diagnostic, {
            "method": "POST",
            "path": "/api/v1/sqllab/execute/",
            "timeout_seconds": 300.0,
        })

    def test_http_debug_log_contains_safe_request_lifecycle(self):
        class Response(io.BytesIO):
            headers = {"Content-Type": "application/json", "Content-Length": "21"}
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with patch("urllib.request.urlopen", return_value=Response(b'{"status":"success"}')):
            with self.assertLogs("sql_lab_extractor.client", level="DEBUG") as captured:
                HttpClient("https://app.example", "csrf-secret", "session=secret").request(
                    "POST", "/api/v1/sqllab/execute/?token=secret", {"sql": "SELECT private"}
                )
        rendered = "\n".join(captured.output)
        self.assertIn("event=http_request method=POST path=/api/v1/sqllab/execute/ timeout_seconds=300", rendered)
        self.assertIn("event=http_response method=POST path=/api/v1/sqllab/execute/ status=200", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("SELECT", rendered)

    def test_run_level_failure_is_persisted_without_page_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore.create(Path(directory), "SELECT 1")
            error = HttpResponseError({"status_code": 200, "body": "bad gateway"})
            store.record_run_failure("count", error)
            payload = json.loads((store.run_dir / "failures" / "run-count.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["stage"], "count")
            self.assertEqual(payload["error"]["status_code"], 200)
            self.assertIn("bad gateway", payload["error"]["body"])


if __name__ == "__main__":
    unittest.main()
