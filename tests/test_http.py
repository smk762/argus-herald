import http.client
import io
from urllib.error import HTTPError

import pytest

from argus_herald.http import HttpClient, HttpError, read_text


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


def test_an_error_body_is_read_only_up_to_a_cap_and_then_closed():
    from argus_herald.http import ERROR_BODY_LIMIT

    class Endless:
        def __init__(self):
            self.asked, self.closed = [], False

        def read(self, amount=-1):
            self.asked.append(amount)
            if amount is None or amount < 0:
                raise MemoryError("read an endless body to the end")
            return b"x" * amount

        def close(self):
            self.closed = True

    client = HttpClient()
    body = Endless()
    client._opener = _Opener(HTTPError("https://example.org/x", 500, "Server Error", {}, body))
    status, payload = client.request("GET", "https://example.org/x")
    assert status == 500 and len(payload) == ERROR_BODY_LIMIT
    assert body.closed

    body = Endless()
    client._opener = _Opener(HTTPError("https://example.org/x", 500, "Server Error", {}, body))
    assert client.request("GET", "https://example.org/x", limit=100) == (500, b"x" * 100)  # the caller's cap too


class _Sized:
    """A 200 whose body is `size` bytes; records how much each read asked for."""

    status = 200

    def __init__(self, size, declared=None):
        self.size, self.asked = size, []
        self.headers = {} if declared is None else {"Content-Length": str(declared)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, amount=-1):
        self.asked.append(amount)
        return b"x" * (self.size if amount < 0 else min(self.size, amount))


def test_read_text_stops_reading_at_the_limit():
    from argus_herald.http import BodyTooLarge  # imported here so this file still collects without the fix

    client = HttpClient()
    endless = _Sized(10_000_000)
    client._opener = _Serves(endless)
    with pytest.raises(BodyTooLarge) as caught:
        read_text(client, "https://example.org/notes.md", limit=100)
    assert endless.asked == [101]  # one byte past the limit is enough to know
    assert caught.value.status != 200 and "more than 100 bytes" in str(caught.value)
    assert isinstance(caught.value, HttpError)  # callers that skip on an HttpError still do

    declared = _Sized(5000, declared=5000)
    client._opener = _Serves(declared)
    with pytest.raises(BodyTooLarge):
        read_text(client, "https://example.org/notes.md", limit=100)
    assert declared.asked == []  # refused on its Content-Length, nothing read

    client._opener = _Serves(_Sized(100))
    assert read_text(client, "https://example.org/notes.md", limit=100) == "x" * 100


def test_read_text_checks_any_other_http_after_the_fact():
    from argus_herald.http import BodyTooLarge

    class Fake:
        def request(self, method, url, body=None, headers=None):
            return 200, b"x" * 101

    with pytest.raises(BodyTooLarge):
        read_text(Fake(), "https://example.org/notes.md", limit=100)
