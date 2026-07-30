import tempfile
import re
import unittest
import unittest.mock
from pathlib import Path

from sql_lab_extractor.config import collect_config, parse_args
from sql_lab_extractor.auth import AuthError, _cookie_metadata, _navigate_sql_lab, _safe_url, _wait_for_login, parse_browser_cookies, resolve_credentials, resolve_profile_dir, submit_login_form
from sql_lab_extractor.client import redact
from sql_lab_extractor.__main__ import build_execute_payload
from sql_lab_extractor.query import QueryError, build_count_query, build_offset_query, execute_query, has_top_level_order_by, interpret_execute


class ConfigTests(unittest.TestCase):
    def test_collects_interactive_config_with_safe_defaults(self):
        answers = iter(["", "", "", "query.sql", "", "", "", "", "", "", ""])
        config = collect_config(input_value=lambda prompt: next(answers))
        self.assertEqual(config.base_url, "https://fasih-dashboard.bps.go.id")
        self.assertEqual(config.database_id, 25)
        self.assertEqual(config.schema, "tcz_37526b20")
        self.assertEqual(config.sql_file, Path("query.sql"))
        self.assertEqual(config.page_size, 1000)
        self.assertEqual(config.query_limit, 100000)
        self.assertEqual(config.sql_editor_id, "951872")
        self.assertEqual(config.tab, "UB_Nilai Tambah")
        self.assertEqual(config.workers, 2)

    def test_arguments_remain_available_for_automation(self):
        config = parse_args(["--sql-file", "q.sql"])
        self.assertEqual(config.sql_file, Path("q.sql"))
        self.assertEqual(config.database_id, 25)


    def test_builds_full_execute_payload_with_fresh_client_id(self):
        config = parse_args(["--sql-file", "q.sql"])
        payload = build_execute_payload(config, "SELECT 1")
        self.assertRegex(payload["client_id"], re.compile(r"^[A-Za-z0-9_-]{10}$"))
        self.assertEqual(payload["sql_editor_id"], "951872")
        self.assertEqual(payload["tab"], "UB_Nilai Tambah")
        self.assertEqual(payload["queryLimit"], 100000)
        self.assertTrue(payload["json"])
        self.assertTrue(payload["expand_data"])
        self.assertFalse(payload["select_as_cta"])
        self.assertEqual(payload["ctas_method"], "TABLE")
        self.assertEqual(payload["tmp_table_name"], "")


    def test_profile_dir_honors_explicit_environment(self):
        with unittest.mock.patch.dict("os.environ", {"SQL_LAB_BROWSER_PROFILE_DIR": "C:/profiles/sql-lab"}, clear=False):
            self.assertEqual(resolve_profile_dir(), Path("C:/profiles/sql-lab"))

    def test_sensitive_local_artifacts_are_ignored(self):
        ignore_file = Path(".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", "sql_lab_browser.log", "*.har", "*.partial"):
            self.assertIn(pattern, ignore_file)

class BrowserDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_remove_query_fragment_and_cookie_values(self):
        self.assertEqual(
            _safe_url("https://identity.example/login?code=secret#token"),
            "https://identity.example/login",
        )
        cookies = [{"name": "session", "value": "secret", "domain": ".app.example"}]
        self.assertEqual(_cookie_metadata(cookies), "session@app.example")
        self.assertNotIn("secret", _cookie_metadata(cookies))




class AuthTests(unittest.TestCase):
    def test_parses_cookie_handoff_and_csrf(self):
        payload = {"cookies": [{"name": "session", "value": "abc", "domain": "app.example"}, {"name": "csrftoken", "value": "csrf", "domain": "app.example"}]}
        session = parse_browser_cookies(payload, "app.example")
        self.assertEqual(session.cookie_header, "session=abc; csrftoken=csrf")
        self.assertEqual(session.csrf_token, "csrf")

    def test_rejects_cookie_handoff_without_csrf(self):
        with self.assertRaises(AuthError):
            parse_browser_cookies({"cookies": [{"name": "session", "value": "abc", "domain": "app.example"}]}, "app.example")
    def test_accepts_xsrf_alias_and_filters_foreign_domain(self):
        payload = {"cookies": [
            {"name": "session", "value": "abc", "domain": "app.example"},
            {"name": "XSRF-TOKEN", "value": "csrf", "domain": ".app.example"},
            {"name": "foreign", "value": "secret", "domain": "evil.example"},
        ]}
        session = parse_browser_cookies(payload, "app.example")
        self.assertEqual(session.cookie_header, "session=abc; XSRF-TOKEN=csrf")
        self.assertEqual(session.csrf_token, "csrf")

    def test_rejects_stale_cookie_while_page_remains_on_login(self):
        class Context:
            def cookies(self):
                return [
                    {"name": "session", "value": "abc", "domain": "app.example"},
                    {"name": "csrftoken", "value": "csrf", "domain": "app.example"},
                ]

        class Page:
            url = "https://app.example/login/"

            def wait_for_timeout(self, timeout_ms):
                pass

        with self.assertRaisesRegex(AuthError, "batas waktu"):
            _wait_for_login(Page(), Context(), "app.example", timeout_ms=0)

    def test_rejects_page_on_foreign_origin(self):
        class Context:
            def cookies(self):
                return [
                    {"name": "session", "value": "abc", "domain": "app.example"},
                    {"name": "csrftoken", "value": "csrf", "domain": "app.example"},
                ]

        class Page:
            url = "https://identity.example/welcome/"

            def wait_for_timeout(self, timeout_ms):
                pass

        with self.assertRaises(AuthError):
            _wait_for_login(Page(), Context(), "app.example", timeout_ms=0)
    def test_fetches_csrf_after_authenticated_welcome_redirect(self):
        class Context:
            def cookies(self):
                return [{"name": "session", "value": "abc", "domain": "app.example"}]

        class Page:
            url = "https://app.example/superset/welcome/"

            def evaluate(self, script):
                return {"status": 200, "body": {"result": "csrf-from-api"}}

            def wait_for_timeout(self, timeout_ms):
                pass

        session = _wait_for_login(Page(), Context(), "app.example", timeout_ms=1_000)
        self.assertEqual(session.cookie_header, "session=abc")
        self.assertEqual(session.csrf_token, "csrf-from-api")
    def test_uses_hidden_csrf_token_without_calling_rejected_endpoint(self):
        class Context:
            def cookies(self):
                return [{"name": "session", "value": "abc", "domain": "app.example"}]

        class Page:
            url = "https://app.example/superset/welcome/"

            def evaluate(self, script):
                self.script = script
                return {"status": 200, "body": {"result": "csrf-from-dom"}, "source": "dom"}

            def wait_for_timeout(self, timeout_ms):
                pass

        page = Page()
        session = _wait_for_login(page, Context(), "app.example", timeout_ms=1_000)
        self.assertEqual(session.csrf_token, "csrf-from-dom")
        self.assertIn("#csrf_token", page.script)

    def test_resolves_complete_environment_credentials(self):
        values = {"SQL_LAB_USERNAME": "operator", "SQL_LAB_PASSWORD": "secret"}
        with unittest.mock.patch.dict("os.environ", values, clear=True):
            self.assertEqual(resolve_credentials(), ("operator", "secret"))

    def test_rejects_partial_environment_credentials(self):
        with unittest.mock.patch.dict("os.environ", {"SQL_LAB_USERNAME": "operator"}, clear=True):
            with self.assertRaisesRegex(AuthError, "harus diisi bersama"):
                resolve_credentials()
    def test_resolves_credentials_from_dotenv_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text('SQL_LAB_USERNAME="operator"\nSQL_LAB_PASSWORD=secret\n', encoding="utf-8")
            with unittest.mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(resolve_credentials(env_path), ("operator", "secret"))

    def test_environment_credentials_override_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("SQL_LAB_USERNAME=file-user\nSQL_LAB_PASSWORD=file-secret\n", encoding="utf-8")
            values = {"SQL_LAB_USERNAME": "env-user", "SQL_LAB_PASSWORD": "env-secret"}
            with unittest.mock.patch.dict("os.environ", values, clear=True):
                self.assertEqual(resolve_credentials(env_path), ("env-user", "env-secret"))

    def test_submits_first_available_login_form_without_exposing_values(self):
        class Page:
            def __init__(self):
                self.filled = []
                self.clicked = []

            def fill(self, selector, value, timeout=None):
                if selector.startswith('input[name'):
                    self.filled.append((selector, value))
                    return
                raise RuntimeError("selector unavailable")

            def click(self, selector, timeout=None):
                if selector == 'button[type="submit"]':
                    self.clicked.append(selector)
                    return
                raise RuntimeError("selector unavailable")

        page = Page()
        submit_login_form(page, "operator", "secret")
        self.assertEqual([value for _, value in page.filled], ["operator", "secret"])
        self.assertEqual(page.clicked, ['button[type="submit"]'])
    def test_login_selector_attempts_use_short_timeout(self):
        class Page:
            def __init__(self):
                self.timeouts = []

            def fill(self, selector, value, timeout):
                self.timeouts.append(timeout)
                if selector == 'input[name="username"]' or selector == 'input[name="password"]':
                    return
                raise RuntimeError("selector unavailable")

            def click(self, selector, timeout):
                self.timeouts.append(timeout)
                if selector == 'button[type="submit"]':
                    return
                raise RuntimeError("selector unavailable")

        page = Page()
        submit_login_form(page, "operator", "secret")
        self.assertTrue(page.timeouts)
        self.assertTrue(all(timeout <= 3_000 for timeout in page.timeouts))

    def test_does_not_navigate_again_when_already_on_sql_lab(self):
        class Page:
            url = "https://app.example/superset/sqllab/"

            def goto(self, *args, **kwargs):
                raise AssertionError("goto must not run")

        _navigate_sql_lab(Page(), "https://app.example")

    def test_navigates_to_sql_lab_when_login_finishes_on_welcome(self):
        class Page:
            url = "https://app.example/superset/welcome/"

            def __init__(self):
                self.target = None

            def goto(self, target, **kwargs):
                self.target = target
                self.url = target

            def wait_for_function(self, *args, **kwargs):
                pass

        page = Page()
        _navigate_sql_lab(page, "https://app.example")
        self.assertEqual(page.target, "https://app.example/superset/sqllab/")

class QueryTests(unittest.TestCase):
    def test_requires_top_level_order_by(self):
        self.assertTrue(has_top_level_order_by("SELECT * FROM sample ORDER BY id"))
        self.assertFalse(has_top_level_order_by("SELECT * FROM (SELECT * FROM sample ORDER BY id) nested"))

    def test_builds_count_bounded_page_without_changing_order(self):
        sql = "SELECT id FROM sample ORDER BY created_at, id"
        paged = build_offset_query(sql, 1000, 2000)
        self.assertEqual(paged, "SELECT id FROM sample ORDER BY created_at, id LIMIT 1000 OFFSET 2000")

    def test_builds_count_query_without_top_level_order_or_limit(self):
        sql = "SELECT assignment_id FROM sample WHERE active = 1 ORDER BY assignment_id LIMIT 1000"
        count_sql = build_count_query(sql)
        self.assertEqual(
            count_sql,
            "SELECT COUNT(1) AS total_rows FROM (SELECT assignment_id FROM sample WHERE active = 1) AS sql_lab_count",
        )

    def test_count_query_keeps_nested_limit(self):
        sql = "SELECT assignment_id FROM (SELECT assignment_id FROM sample LIMIT 5) nested ORDER BY assignment_id"
        self.assertIn("LIMIT 5", build_count_query(sql))


    def test_count_query_counts_rows_without_reconciliation_column(self):
        sql = "SELECT name FROM sample WHERE active = 1 ORDER BY name LIMIT 1000"
        self.assertEqual(
            build_count_query(sql),
            "SELECT COUNT(1) AS total_rows FROM (SELECT name FROM sample WHERE active = 1) AS sql_lab_count",
        )



    def test_redacts_sensitive_values_recursively(self):
        value = redact({"Cookie": "secret", "sql": "SELECT private", "nested": {"rows": [1]}})
        self.assertEqual(value, {"Cookie": "[REDACTED]", "sql": "[REDACTED]", "nested": {"rows": "[REDACTED]"}})




if __name__ == "__main__":
    unittest.main()
