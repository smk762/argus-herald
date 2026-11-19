"""Small HTTPS client. Redirects are not followed."""

from __future__ import annotations

import json
from collections.abc import Mapping
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from argus_herald import __version__

USER_AGENT = f"argus-herald/{__version__} (release announcements; +https://pypi.org)"


class HttpError(Exception):
    def __init__(self, method: str, url: str, status: int):
        host = urlparse(url).hostname or "unknown-host"
        super().__init__(f"{method} {host} failed with HTTP {status}")
        self.status = status
        self.method = method


class BodyTooLarge(HttpError):
    """The response body is over the caller's limit. An HttpError (status 0), so callers that skip on one still do."""

    def __init__(self, method: str, url: str, limit: int):
        super().__init__(method, url, 0)
        self.args = (f"{method} {urlparse(url).hostname or 'unknown-host'} answered with more than {limit} bytes",)
        self.limit = limit


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class Http(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]: ...


def check_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    local = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.username or parsed.password:
        raise ValueError("URLs must not include credentials")
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and local:
        return
    raise ValueError("URLs must use HTTPS")


def read_json(http: Http, url: str) -> dict:
    status, body = http.request("GET", url, headers={"Accept": "application/json"})
    if status != 200:
        raise HttpError("GET", url, status)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise HttpError("GET", url, status)
    return data


def read_text(http: Http, url: str, limit: int = 1_000_000) -> str:
    headers = {"Accept": "text/plain, text/markdown"}
    if isinstance(http, HttpClient):  # stops reading at the limit; another Http is only checked afterwards
        status, body = http.request("GET", url, headers=headers, limit=limit)
    else:
        status, body = http.request("GET", url, headers=headers)
    if status != 200:
        raise HttpError("GET", url, status)
    if len(body) > limit:
        raise BodyTooLarge("GET", url, limit)
    return body.decode("utf-8", "replace")


def send_json(
    http: Http,
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        merged.update(headers)
    return http.request("POST", url, json.dumps(payload).encode("utf-8"), merged)


class HttpClient:
    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[int, bytes]:
        """`limit` caps a successful body: BodyTooLarge is raised without reading past it."""
        check_url(url)
        merged = {"User-Agent": USER_AGENT}
        if headers:
            merged.update(headers)
        request = Request(url, data=body, headers=merged, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if limit is None:
                    return response.status, response.read()
                declared = response.headers.get("Content-Length") or ""
                if declared.isdigit() and int(declared) > limit:
                    raise BodyTooLarge(method, url, limit)
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise BodyTooLarge(method, url, limit)
                return response.status, payload
        except BodyTooLarge:
            raise
        except HTTPError as exc:
            try:
                payload = exc.read()
            except (OSError, HTTPException) as read_exc:
                raise HttpError(method, url, 0) from read_exc
            return exc.code, payload
        except (OSError, HTTPException) as exc:
            # URLError, and what urllib lets through unwrapped: a timeout waiting for headers,
            # a dropped connection, a body cut short.
            raise HttpError(method, url, 0) from exc
