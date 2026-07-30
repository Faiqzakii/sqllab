from __future__ import annotations

import json
import re
import urllib.error
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

SENSITIVE_KEYS = {"authorization", "cookie", "set-cookie", "csrf", "csrf_token", "xsrf-token", "csrftoken", "sql", "rows", "data", "password", "token", "access_token", "refresh_token", "id_token", "session"}
MAX_ERROR_BODY_BYTES = 64 * 1024
TOKEN_PATTERN = re.compile(r"(?i)(bearer\s+|token[=:]\s*|session[=:]\s*|csrf[=:]\s*|password[=:]\s*)[^\s,;\"']+")
logger = logging.getLogger(__name__)



class HttpStatusError(RuntimeError):
    def __init__(self, status_code: int, diagnostic: dict[str, Any] | None = None):
        super().__init__(f"HTTP request failed with status {status_code}")
        self.status_code = status_code
        self.diagnostic = diagnostic or {"status_code": status_code}

class HttpResponseError(RuntimeError):
    def __init__(self, diagnostic: dict[str, Any]):
        super().__init__("HTTP response was malformed")
        self.diagnostic = diagnostic

class HttpTimeoutError(RuntimeError):
    def __init__(self, method: str, path: str, timeout: float):
        self.diagnostic = {"method": method, "path": urlsplit(path).path, "timeout_seconds": timeout}
        super().__init__(f"HTTP request timed out after {timeout:g}s: {method} {urlsplit(path).path}")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value

def _sanitize_error_body(raw_body: bytes, content_type: str) -> Any:
    text = raw_body.decode("utf-8", errors="replace")
    if "json" in content_type.lower():
        try:
            return redact(json.loads(text))
        except json.JSONDecodeError:
            pass
    return TOKEN_PATTERN.sub(lambda match: f"{match.group(1)}[REDACTED]", text)


@dataclass(frozen=True)
class HttpClient:
    base_url: str
    csrf_token: str
    cookie: str
    timeout: float = 300.0

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_path = urlsplit(self.base_url + path).path
        logger.debug("event=http_request method=%s path=%s timeout_seconds=%g", method, safe_path, self.timeout)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "X-CSRFToken": self.csrf_token, "Cookie": self.cookie}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                raw_body = response.read()
                content_type = response.headers.get("Content-Type", "") if response.headers else ""
                try:
                    decoded = json.loads(raw_body)
                except json.JSONDecodeError as error:
                    diagnostic_body = raw_body[:MAX_ERROR_BODY_BYTES]
                    raise HttpResponseError({
                        "status_code": status,
                        "method": method,
                        "path": safe_path,
                        "content_type": content_type,
                        "content_length": response.headers.get("Content-Length") if response.headers else None,
                        "body": _sanitize_error_body(diagnostic_body, content_type),
                        "body_truncated": len(raw_body) > MAX_ERROR_BODY_BYTES,
                    }) from error
        except urllib.error.HTTPError as error:
            raw_body = error.read(MAX_ERROR_BODY_BYTES + 1)
            content_type = error.headers.get("Content-Type", "") if error.headers else ""
            diagnostic = {
                "status_code": error.code,
                "method": method,
                "path": urlsplit(error.url or self.base_url + path).path,
                "content_type": content_type,
                "content_length": error.headers.get("Content-Length") if error.headers else None,
                "body": _sanitize_error_body(raw_body[:MAX_ERROR_BODY_BYTES], content_type),
                "body_truncated": len(raw_body) > MAX_ERROR_BODY_BYTES,
            }
            logger.debug("event=http_error method=%s path=%s status=%d", method, safe_path, error.code)
            raise HttpStatusError(error.code, diagnostic) from error
        except TimeoutError as error:
            logger.warning("event=http_timeout method=%s path=%s timeout_seconds=%g", method, safe_path, self.timeout)
            raise HttpTimeoutError(method, self.base_url + path, self.timeout) from error
        except OSError as error:
            logger.warning("event=http_transport_error method=%s path=%s type=%s", method, safe_path, type(error).__name__)
            raise RuntimeError("HTTP request failed") from error
        logger.debug("event=http_response method=%s path=%s status=%s", method, safe_path, status)
        if not isinstance(decoded, dict):
            raise HttpResponseError({"status_code": status, "method": method, "path": safe_path, "body": redact(decoded), "body_truncated": False})
        return decoded
