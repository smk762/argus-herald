from argus_herald.oauth1 import authorization_header, sign


def test_twitter_signature_example():
    """Vector from the X OAuth 1.0a signature documentation."""

    url = "https://api.twitter.com/1.1/statuses/update.json"
    params = {
        "include_entities": "true",
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
    }
    signature = sign(
        "POST",
        url,
        params,
        "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    )
    assert signature == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="
    header = authorization_header(
        "POST",
        url,
        "xvz1evFS4wEEPTGEFPHBog",
        "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        extra={"status": params["status"], "include_entities": "true"},
        nonce=params["oauth_nonce"],
        timestamp=params["oauth_timestamp"],
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header


def test_base_string_normalises_host_and_default_port():
    from argus_herald.oauth1 import signature_base

    plain = signature_base("POST", "https://api.example.com/x", {"a": "1"})
    assert signature_base("POST", "HTTPS://API.Example.com/x", {"a": "1"}) == plain
    assert signature_base("POST", "https://api.example.com:443/x", {"a": "1"}) == plain
    assert signature_base("POST", "https://user:pw@api.example.com/x", {"a": "1"}) == plain
    assert signature_base("GET", "http://Example.com:80/x", {}) == signature_base("GET", "http://example.com/x", {})
    assert "example.com%3A8443" in signature_base("GET", "https://example.com:8443/x", {})
    assert "%5B%3A%3A1%5D%3A8080" in signature_base("GET", "http://[::1]:8080/x", {})
