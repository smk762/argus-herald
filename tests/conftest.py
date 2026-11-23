import pytest

# A deny-list term that appears in no fixture text. Tests never read the real private files.
DENY_TERM = "zz-private-term"


@pytest.fixture(autouse=True)
def private_denylist(tmp_path, monkeypatch):
    path = tmp_path / "opsec-denylist.txt"
    path.write_text(f"# test deny-list\n{DENY_TERM}\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("ARGUS_HERALD_OPSEC_DENYLIST", str(path))
    # An empty auditor list, so the real one is never read. It has to exist: a list named in
    # the environment but missing makes the gate unavailable.
    auditor = tmp_path / "auditor-denylist.txt"
    auditor.write_text("", encoding="utf-8")
    auditor.chmod(0o600)
    monkeypatch.setenv("AI_AUDIT_OPSEC_DENYLIST", str(auditor))
    return path


@pytest.fixture(autouse=True)
def no_github_token(monkeypatch):
    # A token in the shell switches GitHub polling from the feed to the API; tests that want it pass one.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def no_suite_settings(monkeypatch):
    # A developer's suite file or shell may name a real mint; tests never read either.
    monkeypatch.setenv("ARGUS_ENV_FILE", "off")
    monkeypatch.delenv("ARGUS_MINT_URL", raising=False)


@pytest.fixture
def gate(private_denylist):
    from argus_herald.privacy import load_gate

    return load_gate({})
