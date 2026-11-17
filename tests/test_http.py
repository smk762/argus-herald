import http.client
import io
from urllib.error import HTTPError

import pytest

from argus_herald.http import HttpClient, HttpError


class _Opener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, request, timeout=None):
        raise self.exc


class _Body:
    def __init__(self, exc):
        self.exc = exc

    def read(self, *args):
        raise self.exc

    def close(self):
        pass


class _Response:
    status = 200

    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise self.exc


class _Serves:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout=None):
        return self.response


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        http.client.RemoteDisconnected("closed"),
        http.client.IncompleteRead(b"par"),
    ],
)
def test_network_failures_become_http_errors(exc):
    client = HttpClient()
    client._opener = _Opener(exc)
    with pytest.raises(HttpError) as caught:
        client.request("GET", "https://example.org/x")
    assert caught.value.status == 0
    client._opener = _Serves(_Response(exc))  # the body is cut short
    with pytest.raises(HttpError):
        client.request("GET", "https://example.org/x")


def test_an_error_body_that_cannot_be_read_is_an_http_error():
    client = HttpClient()
    error = HTTPError("https://example.org/x", 502, "Bad Gateway", {}, _Body(TimeoutError("timed out")))
    client._opener = _Opener(error)
    with pytest.raises(HttpError):
        client.request("GET", "https://example.org/x")
    readable = HTTPError("https://example.org/x", 404, "Not Found", {}, io.BytesIO(b"gone"))
    client._opener = _Opener(readable)
    assert client.request("GET", "https://example.org/x") == (404, b"gone")
