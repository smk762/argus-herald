"""OAuth 1.0a HMAC-SHA1 for user-context API calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from urllib.parse import parse_qsl, quote, urlparse


def percent(value: str) -> str:
    return quote(str(value), safe="~")


def signature_base(method: str, url: str, params: Mapping[str, str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "" or parsed.netloc == "":
        raise ValueError("OAuth URL must be absolute")
    # RFC 5849 3.4.1.2: lowercase scheme and host, no default port, no userinfo.
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{port}"
    base_url = f"{scheme}://{host}{parsed.path or '/'}"  # no path is sent as "GET / HTTP/1.1"
    items = [(percent(key), percent(value)) for key, value in params.items()]
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        items.append((percent(key), percent(value)))
    items.sort()
    param_string = "&".join(f"{key}={value}" for key, value in items)
    return "&".join(
        (
            percent(method.upper()),
            percent(base_url),
            percent(param_string),
        )
    )


def sign(
    method: str,
    url: str,
    params: Mapping[str, str],
    consumer_secret: str,
    token_secret: str,
) -> str:
    base = signature_base(method, url, params)
    key = percent(consumer_secret) + "&" + percent(token_secret)
    digest = hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def authorization_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    *,
    extra: Mapping[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    signed = dict(oauth)
    if extra:
        signed.update(extra)
    oauth["oauth_signature"] = sign(method, url, signed, consumer_secret, token_secret)
    pairs = ", ".join(f'{percent(key)}="{percent(value)}"' for key, value in sorted(oauth.items()))
    return f"OAuth {pairs}"
