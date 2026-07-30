from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    from camoufox.sync_api import Camoufox
except ImportError:
    Camoufox = None
logger = logging.getLogger(__name__)

USERNAME_ENV = "SQL_LAB_USERNAME"
PASSWORD_ENV = "SQL_LAB_PASSWORD"
USERNAME_SELECTORS = ('input[name="username"]', 'input[id="username"]', 'input[type="email"]', 'input[type="text"]')
PASSWORD_SELECTORS = ('input[name="password"]', 'input[id="password"]', 'input[type="password"]')
SUBMIT_SELECTORS = ('button[type="submit"]', 'input[type="submit"]')
SSO_REDIRECT_SELECTORS = ('xpath=/html/body/div/div[2]/form/button', 'button[type="submit"]')
DEFAULT_ENV_PATH = Path(".env")
SELECTOR_TIMEOUT_MS = 2_000
LOGIN_FORM_TIMEOUT_MS = 30_000
SESSION_COOKIE_NAMES = {"session", "sessionid", "session_id", "superset_session"}
AUTH_COOKIE_NAMES = {"auth", "auth_token", "access_token", "refresh_token", "remember_token"}
BROWSER_LOG_PATH = Path("sql_lab_browser.log")
BROWSER_LOG_MAX_BYTES = 2 * 1024 * 1024
BROWSER_LOG_BACKUP_COUNT = 3
DEFAULT_PROFILE_ENV = "SQL_LAB_BROWSER_PROFILE_DIR"
DEFAULT_PROFILE_DIR = Path("artifacts/session/profile")


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _cookie_metadata(cookies: list[dict[str, Any]]) -> str:
    return ",".join(
        f"{cookie.get('name')}@{str(cookie.get('domain', '')).lstrip('.')}"
        for cookie in cookies
        if isinstance(cookie.get("name"), str)
    )


def _configure_browser_log() -> logging.Logger:
    browser_logger = logging.getLogger("sql_lab_extractor.browser")
    if not browser_logger.handlers:
        handler = RotatingFileHandler(
            BROWSER_LOG_PATH,
            maxBytes=BROWSER_LOG_MAX_BYTES,
            backupCount=BROWSER_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        browser_logger.addHandler(handler)
        browser_logger.setLevel(logging.INFO)
        browser_logger.propagate = False
    return browser_logger


def _attach_browser_diagnostics(page: Any, browser_logger: logging.Logger) -> None:
    page.on("framenavigated", lambda frame: browser_logger.info("event=navigation url=%s", _safe_url(str(frame.url))))
    page.on("load", lambda: browser_logger.info("event=load url=%s", _safe_url(str(page.url))))
    page.on("console", lambda message: browser_logger.info("event=console type=%s", message.type))
    page.on("pageerror", lambda error: browser_logger.warning("event=page_error type=%s", type(error).__name__))
    page.on("request", lambda request: browser_logger.info("event=request method=%s url=%s", request.method, _safe_url(str(request.url))))
    page.on("response", lambda response: browser_logger.info("event=response status=%s url=%s", response.status, _safe_url(str(response.url))))


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserSession:
    cookie_header: str
    csrf_token: str


def parse_browser_cookies(payload: object, hostname: str) -> BrowserSession:
    cookies = payload.get("cookies") if isinstance(payload, dict) else payload
    if not isinstance(cookies, list):
        raise AuthError("Format cookie browser tidak valid")
    accepted: list[tuple[str, str]] = []
    csrf_token: str | None = None
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name, value, domain = item.get("name"), item.get("value"), str(item.get("domain", "")).lstrip(".")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if hostname != domain and not hostname.endswith(f".{domain}"):
            continue
        accepted.append((name, value))
        if name.lower() in {"csrftoken", "csrf_token", "csrf-token", "xsrf-token", "_xsrf"}:
            csrf_token = value
    if not accepted or not csrf_token:
        raise AuthError("Session browser atau CSRF token tidak ditemukan")
    return BrowserSession("; ".join(f"{name}={value}" for name, value in accepted), csrf_token)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
            normalized = normalized[1:-1]
        values[key.strip()] = normalized
    return values


def resolve_credentials(env_path: Path = DEFAULT_ENV_PATH) -> tuple[str, str] | None:
    environment_username = os.environ.get(USERNAME_ENV)
    environment_password = os.environ.get(PASSWORD_ENV)
    if environment_username or environment_password:
        if not environment_username or not environment_password:
            raise AuthError(f"{USERNAME_ENV} dan {PASSWORD_ENV} harus diisi bersama")
        logger.info("Credentials loaded from environment")
        return environment_username, environment_password
    file_values = _read_dotenv(env_path)
    username = file_values.get(USERNAME_ENV)
    password = file_values.get(PASSWORD_ENV)
    if bool(username) != bool(password):
        raise AuthError(f"{USERNAME_ENV} dan {PASSWORD_ENV} di {env_path} harus diisi bersama")
    if username and password:
        logger.info("Credentials loaded from %s", env_path)
        return username, password
    return None


def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> str:
    for selector in selectors:
        try:
            page.fill(selector, value, timeout=SELECTOR_TIMEOUT_MS)
            return selector
        except Exception:
            continue
    raise AuthError("Form login tidak ditemukan; lanjutkan login secara manual")


def submit_login_form(page: Any, username: str, password: str) -> None:
    _fill_first(page, USERNAME_SELECTORS, username)
    _fill_first(page, PASSWORD_SELECTORS, password)
    for selector in SUBMIT_SELECTORS:
        try:
            page.click(selector, timeout=SELECTOR_TIMEOUT_MS)
            return
        except Exception:
            continue
    raise AuthError("Tombol login tidak ditemukan; lanjutkan login secara manual")
    
    
def resolve_profile_dir() -> Path:
    configured = os.environ.get(DEFAULT_PROFILE_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_PROFILE_DIR.resolve()


def _browser_cookies(context: Any) -> list[dict[str, Any]]:
    cookies = context.cookies()
    if not isinstance(cookies, list):
        raise AuthError("Format cookie browser tidak valid")
    return [cookie for cookie in cookies if isinstance(cookie, dict)]


def _authenticated_cookie_session(page: Any, cookies: list[dict[str, Any]], hostname: str) -> BrowserSession:
    accepted = [
        (str(cookie.get("name")), str(cookie.get("value")))
        for cookie in cookies
        if isinstance(cookie.get("name"), str)
        and cookie.get("value") not in (None, "")
        and (hostname == str(cookie.get("domain", "")).lstrip(".") or hostname.endswith(f".{str(cookie.get('domain', '')).lstrip('.')}"))
    ]
    if not any(name.lower() in SESSION_COOKIE_NAMES | AUTH_COOKIE_NAMES for name, _ in accepted):
        raise AuthError("Session browser belum tersedia")
    token_payload = page.evaluate(
        """async () => {
            const input = document.querySelector('#csrf_token, input[name="csrf_token"]');
            if (input instanceof HTMLInputElement && input.value) {
                return {status: 200, body: {result: input.value}, source: 'dom'};
            }
            const response = await fetch('/api/v1/security/csrf_token/', {
                credentials: 'same-origin',
                headers: {Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            });
            return {status: response.status, body: response.ok ? await response.json() : null, source: 'api'};
        }"""
    )
    if not isinstance(token_payload, dict) or token_payload.get("status") != 200:
        status = token_payload.get("status") if isinstance(token_payload, dict) else "unknown"
        raise AuthError(f"Endpoint CSRF menolak session browser (HTTP {status})")
    token_payload = token_payload.get("body")
    csrf_token = token_payload.get("result") if isinstance(token_payload, dict) else None
    if not isinstance(csrf_token, str) or not csrf_token:
        raise AuthError("CSRF token tidak ditemukan setelah login")
    return BrowserSession("; ".join(f"{name}={value}" for name, value in accepted), csrf_token)


def _wait_for_login(
    page: Any,
    context: Any,
    hostname: str,
    timeout_ms: int = 120_000,
    browser_logger: logging.Logger | None = None,
) -> BrowserSession:
    deadline = time.monotonic() + timeout_ms / 1000
    previous_state: tuple[str, str] | None = None
    while time.monotonic() < deadline:
        current_url = str(page.url)
        parsed_url = urlparse(current_url)
        cookies = _browser_cookies(context)
        state = (_safe_url(current_url), _cookie_metadata(cookies))
        if browser_logger is not None and state != previous_state:
            browser_logger.info("event=auth_state url=%s cookies=%s", state[0], state[1] or "none")
            previous_state = state
        if parsed_url.hostname == hostname and "/login" not in parsed_url.path.lower():
            try:
                session = _authenticated_cookie_session(page, cookies, hostname)
                if browser_logger is not None:
                    browser_logger.info("event=csrf_fetch status=success")
                return session
            except Exception as error:
                if browser_logger is not None:
                    browser_logger.info("event=csrf_fetch status=pending error_type=%s", type(error).__name__)
        page.wait_for_timeout(500)
    raise AuthError("Login SSO/MFA belum selesai dalam batas waktu")


def _is_sql_lab_url(url: str) -> bool:
    return urlparse(url).path.rstrip("/").endswith("/superset/sqllab")


def _navigate_sql_lab(page: Any, base_url: str) -> None:
    if _is_sql_lab_url(str(page.url)):
        logger.info("SQL Lab already active; skipping duplicate navigation")
        return
    target = f"{base_url.rstrip('/')}/superset/sqllab/"
    page.goto(target, wait_until="commit", timeout=180_000)
    page.wait_for_function(
        "() => window.location.pathname.includes('/superset/sqllab/')",
        timeout=30_000,
    )


def bootstrap_browser_session(base_url: str, profile_dir: Path | None = None) -> BrowserSession:
    hostname = urlparse(base_url).hostname
    if not hostname:
        raise AuthError("Base URL tidak valid")
    if Camoufox is None:
        raise AuthError("Camoufox belum terpasang; jalankan pip install camoufox lalu camoufox fetch")
    browser_logger = _configure_browser_log()
    profile_dir = (Path(profile_dir).expanduser().resolve() if profile_dir is not None else resolve_profile_dir())
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_logger.info("event=browser_start host=%s profile=%s", hostname, "configured" if (profile_dir is not None or os.environ.get(DEFAULT_PROFILE_ENV)) else "default")
    manager = Camoufox(headless=False, os="windows", locale="id-ID", humanize=True, block_webrtc=True, enable_cache=True, persistent_context=True, user_data_dir=str(profile_dir))
    context = manager.__enter__()
    try:
        page = context.new_page()
        _attach_browser_diagnostics(page, browser_logger)
        page.goto(f"{base_url.rstrip('/')}/login/", wait_until="domcontentloaded", timeout=180_000)
        logger.info("Camoufox opened login page", extra={"event": "auth_browser_opened", "host": hostname})
        credentials = resolve_credentials()
        if credentials:
            try:
                clicked_sso = False
                for selector in SSO_REDIRECT_SELECTORS:
                    try:
                        page.click(selector, timeout=SELECTOR_TIMEOUT_MS)
                        clicked_sso = True
                        logger.info("SSO login button clicked; waiting for credential form")
                        break
                    except Exception:
                        continue
                if clicked_sso:
                    page.wait_for_selector(PASSWORD_SELECTORS[0], timeout=LOGIN_FORM_TIMEOUT_MS)
                submit_login_form(page, *credentials)
                logger.info("Login form submitted; waiting for MFA or application redirect", extra={"event": "auth_form_submitted"})
            except AuthError as error:
                logger.warning("Automatic login unavailable; falling back to manual login", extra={"event": "auth_manual_fallback", "reason": str(error)})
        else:
            logger.info("Credentials unavailable; waiting for manual login", extra={"event": "auth_manual_required"})
        session = _wait_for_login(page, context, hostname, browser_logger=browser_logger)
        logger.info("Authenticated redirect detected: path=%s", urlparse(str(page.url)).path)
        _navigate_sql_lab(page, base_url)
        return session
    except AuthError:
        raise
    except Exception as error:
        browser_logger.exception("event=browser_error path=%s type=%s", urlparse(str(getattr(page, "url", ""))).path, type(error).__name__)
        raise AuthError(f"Login browser gagal ({type(error).__name__}: {error})") from error
    finally:
        try:
            context.close()
        finally:
            manager.__exit__(None, None, None)


