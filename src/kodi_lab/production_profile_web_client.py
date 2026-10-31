"""Test-only canonical Web client for the disposable production profile.

This module deliberately uses only the public browser surface.  It is not an
application authentication adapter and must never be imported by Web routes.
"""

from __future__ import annotations

from contextlib import suppress
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from types import MappingProxyType
from typing import Final, Protocol, cast
from urllib.parse import urlsplit

import httpx

PRODUCTION_PROFILE_BASE_URL: Final = "http://10.251.0.10:8180"
PRODUCTION_PROFILE_HOST: Final = "10.251.0.10:8180"
SESSION_COOKIE_NAME: Final = "kodi_lab_session"
_CURRENT_CSRF = object()
_CANONICAL_ORIGIN = object()


class _SyncClient(Protocol):
    cookies: httpx.Cookies

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response: ...

    def post(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json: object | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response: ...

    def close(self) -> None: ...


class ProductionProfileWebClientError(RuntimeError):
    """A bounded failure that never embeds request secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuthenticationRejected(ProductionProfileWebClientError):
    """The canonical login or an authenticated request was rejected."""


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_login_form = False
        self.login_form_seen = False
        self.password_field_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            # An omitted action submits to the known current /login URL.
            self.in_login_form = (
                attributes.get("method") or ""
            ).lower() == "post" and attributes.get("action", "/login") == "/login"
            self.login_form_seen = self.login_form_seen or self.in_login_form
        elif tag == "input" and self.in_login_form:
            if (
                attributes.get("name") == "password"
                and (attributes.get("type") or "").lower() == "password"
            ):
                self.password_field_seen = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_login_form = False


class _CsrfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "kodi-lab-csrf":
            self._append(attributes.get("content"))
        if tag == "input" and attributes.get("name") == "csrf_token":
            self._append(attributes.get("value"))
        self._append(attributes.get("data-csrf-token"))
        self._append(attributes.get("data-csrf"))

    def _append(self, token: str | None) -> None:
        if token:
            self.tokens.append(token)


class ProductionProfileWebClient:
    """In-memory browser-equivalent client for the isolated fixture only."""

    def __init__(
        self,
        *,
        client: _SyncClient | None = None,
        base_url: str = PRODUCTION_PROFILE_BASE_URL,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("production-profile Web base URL is invalid")
        if client is None and base_url != PRODUCTION_PROFILE_BASE_URL:
            raise ValueError("live production-profile Web base URL is fixed")
        self._base_url = base_url.rstrip("/")
        self._host = parsed.netloc
        self._origin = self._base_url
        self._client: _SyncClient = client or httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._csrf_token: str | None = None
        self._authenticated = False
        self._closed = False
        self._evidence: dict[str, object] = {}

    def __enter__(self) -> ProductionProfileWebClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def evidence(self) -> MappingProxyType[str, object]:
        """Return only bounded, non-secret semantic diagnostics."""

        return MappingProxyType(dict(self._evidence))

    def security_preflight(self) -> MappingProxyType[str, object]:
        """Prove login form, Host policy, and absent/invalid cookie behavior."""

        self._require_open()
        self._client.cookies.clear()
        login = self._get("/login")
        self._require_login_form(login)
        unexpected_host = self._get("/login", host="attacker.invalid")
        missing = self._get("/api/instances")
        malformed = self._get("/api/instances", cookie_value="not-a-valid-session")
        invalid = self._get("/api/instances", cookie_value="A" * 43)
        self._client.cookies.clear()
        if unexpected_host.status_code != 400:
            raise ProductionProfileWebClientError("trusted_host_not_rejected")
        if any(
            response.status_code != 401 for response in (missing, malformed, invalid)
        ):
            raise ProductionProfileWebClientError("invalid_session_not_rejected")
        self._evidence.update(
            {
                "login_page_status": login.status_code,
                "canonical_host_accepted": True,
                "unexpected_host_rejected": True,
                "missing_session_rejected": True,
                "malformed_session_rejected": True,
                "invalid_session_rejected": True,
            }
        )
        return self.evidence

    def login(self, password: str) -> None:
        """Perform the exact Origin-checked password-form login."""

        self._require_open()
        self._client.cookies.clear()
        self._csrf_token = None
        self._authenticated = False
        login_page = self._get("/login")
        self._require_login_form(login_page)
        response = self._post(
            "/login",
            data={"password": password},
            headers={"Origin": self._origin},
        )
        if response.status_code == 401:
            self._evidence["login_succeeded"] = False
            raise AuthenticationRejected("login_rejected")
        if response.status_code != 303 or response.headers.get("location") != "/kodi":
            raise ProductionProfileWebClientError("login_contract_mismatch")
        self._validate_session_cookie(response)
        self._authenticated = True
        self._evidence.update(
            {
                "login_succeeded": True,
                "login_redirect_status": response.status_code,
                "session_cookie_in_memory": True,
            }
        )

    def get_authenticated(self, path: str) -> httpx.Response:
        """Load one authenticated relative path and retain any rendered CSRF token."""

        self._require_authenticated()
        response = self._get(path)
        if response.status_code == 401 or (
            response.status_code == 303 and response.headers.get("location") == "/login"
        ):
            self._authenticated = False
            raise AuthenticationRejected("session_rejected")
        if response.status_code != 200:
            raise ProductionProfileWebClientError("authenticated_get_failed")
        self._retain_rendered_csrf(response)
        return response

    def csrf_token(self) -> str:
        """Return the in-memory synchronizer token rendered by an authenticated page."""

        self._require_authenticated()
        if self._csrf_token is None:
            self.get_authenticated("/kodi")
        if self._csrf_token is None:
            raise ProductionProfileWebClientError("csrf_token_missing")
        return self._csrf_token

    def post_authenticated(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        json: object | None = None,
        csrf_token: str | object | None = _CURRENT_CSRF,
        origin: str | object | None = _CANONICAL_ORIGIN,
    ) -> httpx.Response:
        """POST using ordinary session, Origin, and header-CSRF mechanics.

        Passing ``None`` for either security input deliberately omits it, which
        supports negative harness proofs without introducing a server bypass.
        """

        self._require_authenticated()
        if data is not None and json is not None:
            raise ValueError("form and JSON bodies are mutually exclusive")
        headers: dict[str, str] = {}
        if origin is _CANONICAL_ORIGIN:
            headers["Origin"] = self._origin
        elif isinstance(origin, str):
            headers["Origin"] = origin
        if csrf_token is _CURRENT_CSRF:
            headers["X-Kodi-Lab-CSRF"] = self.csrf_token()
        elif isinstance(csrf_token, str):
            headers["X-Kodi-Lab-CSRF"] = csrf_token
        response = self._post(path, data=data, json=json, headers=headers)
        if response.status_code == 401:
            self._authenticated = False
            raise AuthenticationRejected("session_rejected")
        return response

    def logout(self) -> None:
        """Log out normally and prove the retired cookie cannot authenticate."""

        self._require_authenticated()
        prior_cookie = self._session_cookie_value()
        response = self._post(
            "/logout",
            data={"csrf_token": self.csrf_token()},
            headers={"Origin": self._origin},
        )
        if response.status_code != 303 or response.headers.get("location") != "/login":
            raise ProductionProfileWebClientError("logout_contract_mismatch")
        if self._session_cookie_value(required=False) is not None:
            raise ProductionProfileWebClientError("logout_cookie_not_deleted")
        replay = self._get("/api/instances", cookie_value=prior_cookie)
        if replay.status_code != 401:
            raise ProductionProfileWebClientError("logged_out_session_reused")
        self._client.cookies.clear()
        self._csrf_token = None
        self._authenticated = False
        self._evidence.update(
            {
                "logout_succeeded": True,
                "logged_out_session_rejected": True,
                "session_cookie_in_memory": False,
            }
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._authenticated:
            with suppress(ProductionProfileWebClientError, httpx.HTTPError):
                self.logout()
        self._client.cookies.clear()
        self._csrf_token = None
        if self._owns_client:
            self._client.close()
        self._closed = True

    def _get(
        self,
        path: str,
        *,
        host: str | None = None,
        cookie_value: str | None = None,
    ) -> httpx.Response:
        self._validate_path(path)
        headers = {"Host": self._host if host is None else host}
        if cookie_value is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie_value}"
        return self._client.get(path, headers=headers, follow_redirects=False)

    def _post(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        json: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self._validate_path(path)
        request_headers = {"Host": self._host}
        request_headers.update(headers or {})
        return self._client.post(
            path,
            data=data,
            json=json,
            headers=request_headers,
            follow_redirects=False,
        )

    @staticmethod
    def _validate_path(path: str) -> None:
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
        ):
            raise ValueError("Web client paths must be relative absolute paths")

    @staticmethod
    def _require_login_form(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise ProductionProfileWebClientError("login_page_unavailable")
        parser = _LoginFormParser()
        parser.feed(response.text)
        if not parser.login_form_seen or not parser.password_field_seen:
            raise ProductionProfileWebClientError("login_form_contract_mismatch")

    def _validate_session_cookie(self, response: httpx.Response) -> None:
        parsed = SimpleCookie()
        parsed.load(response.headers.get("set-cookie", ""))
        if set(parsed) != {SESSION_COOKIE_NAME}:
            raise ProductionProfileWebClientError("session_cookie_contract_mismatch")
        morsel = parsed[SESSION_COOKIE_NAME]
        if (
            morsel["path"] != "/"
            or morsel["domain"]
            or morsel["secure"]
            or not morsel["httponly"]
            or morsel["samesite"].lower() != "strict"
            or morsel["max-age"] != "604800"
        ):
            raise ProductionProfileWebClientError("session_cookie_policy_mismatch")
        cookies = [
            cookie
            for cookie in self._client.cookies.jar
            if cookie.name == SESSION_COOKIE_NAME
        ]
        if (
            len(cookies) != 1
            or cookies[0].path != "/"
            or cookies[0].secure
            or not cookies[0].value
        ):
            raise ProductionProfileWebClientError("session_cookie_not_retained")

    def _retain_rendered_csrf(self, response: httpx.Response) -> None:
        parser = _CsrfParser()
        parser.feed(response.text)
        if not parser.tokens:
            return
        tokens = set(parser.tokens)
        if len(tokens) != 1:
            raise ProductionProfileWebClientError("csrf_token_inconsistent")
        token = next(iter(tokens))
        if self._csrf_token is not None and self._csrf_token != token:
            raise ProductionProfileWebClientError("csrf_token_rotated_unexpectedly")
        self._csrf_token = token

    def _session_cookie_value(self, *, required: bool = True) -> str | None:
        cookies = [
            cookie.value
            for cookie in self._client.cookies.jar
            if cookie.name == SESSION_COOKIE_NAME
        ]
        if len(cookies) == 1:
            return cast(str, cookies[0])
        if required:
            raise ProductionProfileWebClientError("session_cookie_absent")
        return None

    def _require_authenticated(self) -> None:
        self._require_open()
        if not self._authenticated:
            raise AuthenticationRejected("session_absent")

    def _require_open(self) -> None:
        if self._closed:
            raise ProductionProfileWebClientError("client_closed")
