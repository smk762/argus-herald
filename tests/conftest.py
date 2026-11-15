import pytest

# A deny-list term that appears in no fixture text. Tests never read the real private files.
DENY_TERM = "zz-private-term"


@pytest.fixture(autouse=True)
def private_denylist(tmp_path, monkeypatch):
    path = tmp_path / "opsec-denylist.txt"
    path.write_text(f"# test deny-list\n{DENY_TERM}\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("PIP_RELEASE_OPSEC_DENYLIST", str(path))
    monkeypatch.setenv("AI_AUDIT_OPSEC_DENYLIST", str(tmp_path / "no-auditor-list.txt"))
    return path


@pytest.fixture
def gate(private_denylist):
    from pip_release.privacy import load_gate

    return load_gate({})
