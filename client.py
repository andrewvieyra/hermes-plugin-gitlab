"""Thin GitLab REST client used by every handler.

Deliberately small: token auth (``PRIVATE-TOKEN`` for personal, project and group access tokens,
``Bearer`` for OAuth), JSON in/out, page-based pagination driven by GitLab's ``x-next-page`` header,
one retry on 429 honouring ``Retry-After``, and typed errors that never carry the token. Resource
knowledge lives in :mod:`targets` (paths) and :mod:`handlers`; this module only speaks HTTP.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

API_PREFIX = "api/v4"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRY_AFTER = 5.0
_PATH_RE = re.compile(r"^[A-Za-z0-9_.%~\-]+(?:/[A-Za-z0-9_.%~\-]+)*$")


class GitLabError(Exception):
    """Any failed GitLab request. ``status`` is the HTTP status (0 for transport errors)."""

    def __init__(self, message: str, status: int = 0, body: Any = None, method: str = "", path: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.method = method
        self.path = path

    def field_errors(self) -> Dict[str, Any]:
        """``{field: [messages]}`` from a validation body (``{"message": {"title": [...]}}``), else ``{}``."""
        if isinstance(self.body, dict) and isinstance(self.body.get("message"), dict):
            return dict(self.body["message"])
        return {}

    def to_dict(self) -> Dict[str, Any]:
        body = self.body
        if not isinstance(body, (dict, list)):
            body = str(body)[:500] if body else None
        return {"message": str(self), "status": self.status, "method": self.method, "path": self.path, "body": body}


class ConfigError(GitLabError):
    """Missing or invalid GITLAB_URL / GITLAB_TOKEN."""


def error_summary(body: Any) -> str:
    """One line from GitLab's error shapes: ``{"message": "..."}``, ``{"message": {field: [...]}}``,
    ``{"error": "...", "error_description": "..."}`` or plain text."""
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, dict):
            return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in message.items())
        if isinstance(message, str):
            return message
        if body.get("error_description"):
            return str(body["error_description"])
        if body.get("error"):
            return str(body["error"])
        return str(body)[:400]
    if isinstance(body, list):
        return "; ".join(str(x) for x in body)[:400]
    return (str(body).strip()[:400]) if body else ""


def auth_headers(token: str) -> Dict[str, str]:
    """``PRIVATE-TOKEN`` for access tokens (the documented header for personal, project and group
    tokens); ``Authorization: Bearer`` when the value is given as ``Bearer <oauth-token>``."""
    token = token.strip()
    if token.lower().startswith("bearer "):
        return {"Authorization": "Bearer " + token[7:].strip()}
    return {"PRIVATE-TOKEN": token}


def verify_from_env(value: Optional[str]) -> Any:
    """``GITLAB_VERIFY_SSL``: unset/true -> verify; false/0/no/off -> do not verify; anything else is a
    path to a CA bundle (self-managed instances with a private CA)."""
    if value is None or not value.strip():
        return True
    text = value.strip()
    if text.lower() in {"1", "true", "yes", "on"}:
        return True
    if text.lower() in {"0", "false", "no", "off"}:
        return False
    return os.path.expanduser(text)


def settings_from_env(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Resolve connection settings from the environment; raises ConfigError when incomplete."""
    env = os.environ if env is None else env
    url = (env.get("GITLAB_URL") or "").strip().rstrip("/")
    token = (env.get("GITLAB_TOKEN") or "").strip()
    if not url or not token:
        raise ConfigError("GITLAB_URL and GITLAB_TOKEN must both be set (put them in ~/.hermes/.env)")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"GITLAB_URL must start with http:// or https:// (got {url!r})")
    if url.endswith("/api/v4"):
        url = url[: -len("/api/v4")]
    try:
        timeout = float(env.get("GITLAB_TIMEOUT") or _DEFAULT_TIMEOUT)
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    return {"url": url, "token": token, "verify": verify_from_env(env.get("GITLAB_VERIFY_SSL")), "timeout": timeout}


def is_configured(env: Optional[Dict[str, str]] = None) -> bool:
    try:
        settings_from_env(env)
        return True
    except GitLabError:
        return False


def encode(segment: Any) -> str:
    """URL-encode one path segment the way GitLab expects (``group/project`` -> ``group%2Fproject``)."""
    return quote(str(segment), safe="")


def project_id(project: Any) -> str:
    """The ``:id`` segment for ``/projects/:id``: a numeric id as-is, a path URL-encoded."""
    if isinstance(project, bool) or project is None:
        raise GitLabError("project is required")
    if isinstance(project, int):
        return str(project)
    text = str(project).strip().strip("/")
    if not text:
        raise GitLabError("project is required")
    return text if text.isdigit() else encode(text)


def validate_path(path: Any) -> str:
    """Normalise a raw API path (``/api/v4/projects/1/releases`` or ``projects/1/releases``) to the part
    after ``/api/v4/``; reject anything with a host, a query string, traversal or odd characters."""
    if not isinstance(path, str):
        raise GitLabError(f"path must be a string (got {type(path).__name__})")
    clean = path.strip()
    if clean.lower().startswith(("http://", "https://", "//")):
        raise GitLabError("path must be relative to /api/v4 on the configured GitLab host (no scheme or host)")
    clean = clean.lstrip("/")
    if clean.startswith(f"{API_PREFIX}/"):
        clean = clean[len(API_PREFIX) + 1 :]
    elif clean == API_PREFIX:
        clean = ""
    clean = clean.rstrip("/")
    if not clean:
        raise GitLabError("path is required, e.g. 'projects/1/releases'")
    if "?" in clean or "#" in clean:
        raise GitLabError("put query parameters in 'params', not in the path")
    if any(segment in {"..", "."} for segment in clean.split("/")):
        raise GitLabError(f"invalid path {path!r}")
    if not _PATH_RE.match(clean):
        raise GitLabError(f"invalid path {path!r}")
    return clean


class GitLabClient:
    """``session`` is anything with ``request(method, url, params=, json=, headers=, timeout=, verify=)``
    returning an object with ``status_code``, ``text``, ``headers`` and ``json()`` — ``requests.Session``
    in production, a fake in tests. ``sleep`` is the 429 back-off (injectable for tests)."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify: Any = True,
        timeout: float = _DEFAULT_TIMEOUT,
        session: Any = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.base_url = url.rstrip("/")
        self.verify = verify
        self.timeout = timeout
        self._token = token.strip()
        self._headers = dict(auth_headers(token))
        self._headers["Accept"] = "application/json"
        if session is None:
            import requests  # imported lazily so plugin registration never needs network libs

            session = requests.Session()
        self._session = session
        self._sleep = sleep or time.sleep
        self.last_call: Dict[str, Any] = {}
        self.last_headers: Dict[str, str] = {}

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None, session: Any = None) -> GitLabClient:
        s = settings_from_env(env)
        return cls(s["url"], s["token"], verify=s["verify"], timeout=s["timeout"], session=session)

    # -- token hygiene ------------------------------------------------------------------------
    def scrub(self, text: Any) -> str:
        """Replace the token wherever it might have leaked into a message (defence in depth)."""
        text = str(text)
        for secret in (self._token, self._headers.get("PRIVATE-TOKEN"), self._headers.get("Authorization", "")[7:]):
            if secret and len(secret) >= 8:
                text = text.replace(secret, "***")
        return text

    def scrub_obj(self, value: Any) -> Any:
        """:meth:`scrub` applied recursively to strings inside dicts and lists (error bodies)."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self.scrub_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_obj(v) for v in value]
        return value

    # -- low level ---------------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{API_PREFIX}/{path.strip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        raw: bool = False,
        _retry: bool = True,
    ) -> Any:
        """One HTTP call. Returns the parsed JSON body (or the text when ``raw``); raises GitLabError on
        transport failure or any 4xx/5xx. Retries once on 429 after ``Retry-After`` (capped)."""
        path = path.strip("/")
        url = self._url(path)
        headers = dict(self._headers)
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self._session.request(
                method, url, params=params or None, json=json, headers=headers, timeout=self.timeout, verify=self.verify
            )
        except Exception as exc:  # transport failure — DNS, TLS, refused, timeout
            self.last_call = {"method": method, "path": f"/{API_PREFIX}/{path}", "status": 0}
            raise GitLabError(self.scrub(f"{method} {url} failed: {exc}"), method=method, path=path) from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        self.last_headers = {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", None) or {}).items()}
        self.last_call = {"method": method, "path": f"/{API_PREFIX}/{path}", "status": status}
        if status == 429 and _retry:
            try:
                wait = float(self.last_headers.get("retry-after") or 1.0)
            except ValueError:
                wait = 1.0
            self._sleep(max(0.0, min(wait, _MAX_RETRY_AFTER)))
            return self.request(method, path, params=params, json=json, raw=raw, _retry=False)
        text = getattr(resp, "text", "") or ""
        body: Any = None
        if raw:
            body = text
        elif text:
            try:
                body = resp.json()
            except Exception:
                body = text
        if status >= 400:
            summary = error_summary(body if not raw else text)
            raise GitLabError(
                self.scrub(f"{method} /{API_PREFIX}/{path} -> HTTP {status}: {summary}"),
                status=status,
                body=self.scrub_obj(body if not raw else text[:500]),
                method=method,
                path=path,
            )
        return body

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)

    def paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        max_results: int = 100,
        per_page: Optional[int] = None,
    ) -> Tuple[List[Any], Optional[int], bool]:
        """Follow ``x-next-page`` until *max_results* items are collected. Returns
        ``(items[:max_results], total_or_None, truncated)``. ``total`` is ``None`` when GitLab omits
        ``x-total`` (it does for expensive lists such as commits)."""
        max_results = max(1, int(max_results))
        query = dict(params or {})
        page = int(query.pop("page", 1) or 1)
        query["per_page"] = per_page or min(100, max_results)
        results: List[Any] = []
        total: Optional[int] = None
        more = False
        while True:
            query["page"] = page
            body = self.get(path, query)
            if not isinstance(body, list):
                raise GitLabError(f"GET /{API_PREFIX}/{path} did not return a list", path=path)
            results.extend(body)
            total_header = self.last_headers.get("x-total", "")
            if total_header.isdigit():
                total = int(total_header)
            next_page = self.last_headers.get("x-next-page", "")
            more = bool(body) and next_page.isdigit()
            if len(results) >= max_results or not more:
                break
            page = int(next_page)
        if total is not None:
            truncated = total > max_results
        else:
            truncated = len(results) > max_results or (len(results) >= max_results and more)
        return results[:max_results], total, truncated

    # -- convenience --------------------------------------------------------------------------
    def version(self) -> Dict[str, Any]:
        body = self.get("version")
        return body if isinstance(body, dict) else {}

    def current_user(self) -> Dict[str, Any]:
        body = self.get("user")
        return body if isinstance(body, dict) else {}

    def token_self(self) -> Optional[Dict[str, Any]]:
        """The token's own record (scopes, expiry). ``None`` when the instance or token type does not
        support it (OAuth tokens, old GitLab)."""
        try:
            body = self.get("personal_access_tokens/self")
        except GitLabError as exc:
            if exc.status in (401, 403, 404, 405):
                return None
            raise
        return body if isinstance(body, dict) else None

    def project(self, project: Any) -> Dict[str, Any]:
        body = self.get(f"projects/{project_id(project)}")
        if not isinstance(body, dict):
            raise GitLabError("GET project did not return an object")
        return body

    def user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        body = self.get("users", {"username": username.strip().lstrip("@")})
        if isinstance(body, list) and body and isinstance(body[0], dict):
            return body[0]
        return None
