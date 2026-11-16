import pytest
from conftest import DENY_TERM

from argus_herald.models import APPROVED, PENDING
from argus_herald.platforms import TwitterPublisher
from argus_herald.privacy import GateUnavailable, load_gate, segments
from argus_herald.store import Store
from argus_herald.workflow import PrivacyRefused, ReviewError, accept_hit, approve, edit_draft, publish_draft

# Public, well-known addresses (the Bitcoin genesis address and a documented bech32 example).
GENESIS = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BECH32 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
EVM = "0x" + "ab" * 20


def rules(gate, text):
    return [(hit.rule, hit.match) for hit in gate.scan(text)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("served from 192.168.0.10.", [("private-ip", "192.168.0.10")]),
        ("10.1.2.3 and 172.16.5.4", [("private-ip", "10.1.2.3"), ("private-ip", "172.16.5.4")]),
        ("link-local 169.254.1.1", [("private-ip", "169.254.1.1")]),
        ("ula fd12:3456::1", [("private-ip", "fd12:3456::1")]),
        ("public 8.8.8.8, loopback 127.0.0.1, version 1.2.3.4", []),
        ("see /home/alice/project", [("home-path", "/home/alice")]),
        (r"C:\Users\bob\file", [("home-path", "C:\\Users\\")]),
        ("mail bob@corp.test", [("email", "bob@corp.test")]),
        ("alice@example.org and 1+dev@users.noreply.github.com", []),
        ("host printer.lan answered", [("lan-host", "printer.lan")]),
        (f"tip {EVM}", [("crypto-address", EVM)]),
        (f"btc {GENESIS}", [("crypto-address", GENESIS)]),
        (f"btc {GENESIS[:-1]}b", []),  # bad checksum: not an address
        (f"bech32 {BECH32}", [("crypto-address", BECH32)]),
        ("send to example-name.eth", [("crypto-address", "example-name.eth")]),
        ("Date: 17:58:06 2026 -0500", [("tz-offset", "-0500")]),
        ("at 2026-11-15T09:00:00+05:30 it broke", [("tz-offset", "+05:30")]),
        ("UTC+5 evening", [("tz-offset", "UTC+5")]),
        ("2026-11-15T09:00:00+00:00 or 09:00:00Z or UTC+0", []),
        (f"from {DENY_TERM.upper()} today", [("deny-term", DENY_TERM.upper())]),
        (f"x{DENY_TERM}y", []),  # part of a longer word
        ("argus-atlas 0.2.0 is out. https://pypi.org/project/argus-atlas/0.2.0/", []),
    ],
)
def test_builtin_checks(gate, text, expected):
    assert rules(gate, text) == expected


def test_allow_list_and_custom_patterns(private_denylist):
    gate = load_gate({"allow_addresses": [EVM.upper().replace("0X", "0x")], "patterns": {"ticket": r"TICKET-\d+"}})
    assert rules(gate, f"contract {EVM}, see TICKET-42") == [("pattern:ticket", "TICKET-42")]
    with pytest.raises(GateUnavailable, match="not a valid regex"):
        load_gate({"patterns": {"bad": "("}})


def test_fails_closed_without_the_denylist(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HERALD_OPSEC_DENYLIST", str(tmp_path / "missing.txt"))
    with pytest.raises(GateUnavailable, match="deny-list is missing"):
        load_gate({})


def test_merges_the_auditor_list_and_warns_on_loose_permissions(private_denylist, tmp_path, monkeypatch):
    auditor = tmp_path / "auditor.txt"
    auditor.write_text("other-secret # comment\n", encoding="utf-8")
    auditor.chmod(0o600)
    monkeypatch.setenv("AI_AUDIT_OPSEC_DENYLIST", str(auditor))
    private_denylist.chmod(0o644)
    gate = load_gate({})
    assert rules(gate, "an other-secret here") == [("deny-term", "other-secret")]
    assert any("chmod 600" in warning for warning in gate.warnings)


def test_segments_mark_hits_and_skip_overlaps(gate):
    text = "a bob@corp.test b"
    hits = gate.scan(text)
    assert segments(text, hits) == [("a ", None), ("bob@corp.test", hits[0]), (" b", None)]


def _store_with(tmp_path, body):
    store = Store(tmp_path / "state.sqlite")
    draft_id, _ = store.add_draft(
        project="argus-atlas",
        version="1",
        platform="twitter",
        title="",
        body=body,
        link="https://pypi.org/project/argus-atlas/1/",
    )
    return store, draft_id


def test_approve_refuses_until_edited_or_accepted(tmp_path, gate):
    store, draft_id = _store_with(tmp_path, f"ping bob@corp.test or {EVM}")
    with pytest.raises(PrivacyRefused) as refused:
        approve(store, TwitterPublisher(), draft_id, None, gate=gate)
    assert {hit.rule for hit in refused.value.hits} == {"email", "crypto-address"}
    assert store.get(draft_id).status == PENDING

    with pytest.raises(ReviewError, match="needs a note"):
        accept_hit(store, gate, draft_id, f"crypto-address:{EVM}", "  ")
    with pytest.raises(ReviewError, match="no open privacy hit"):
        accept_hit(store, gate, draft_id, "email:someone@else.test", "why")
    accept_hit(store, gate, draft_id, f"crypto-address:{EVM}", "the public demo contract")
    edit_draft(store, TwitterPublisher(), draft_id, "", f"ping us or {EVM}")
    assert approve(store, TwitterPublisher(), draft_id, None, gate=gate).status == APPROVED
    assert store.accepted(draft_id) == {f"crypto-address:{EVM}": "the public demo contract"}


def test_publish_checks_again(tmp_path, gate, private_denylist):
    store, draft_id = _store_with(tmp_path, "argus-atlas 1 is out, built on newterm")
    approve(store, TwitterPublisher(), draft_id, None, gate=gate)
    private_denylist.write_text(f"{DENY_TERM}\nnewterm\n", encoding="utf-8")
    with pytest.raises(PrivacyRefused):
        publish_draft(store, TwitterPublisher(), store.get(draft_id), {}, None, gate=load_gate({}))
    assert store.get(draft_id).status == APPROVED
