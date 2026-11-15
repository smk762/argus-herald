import json
import types

import pytest

from pip_release.config import Audience, Destination, DraftingSettings, ConfigError, load_config
from pip_release.digest import build_digest, regenerate
from pip_release.drafting import (
    AnthropicBackend, Backend, DraftingError, OpenAICompatible, Request, backend_from, clean, configured_backend,
    draft, problems,
)
from pip_release.platforms import PatreonPublisher, TwitterPublisher
from pip_release.store import Store

from conftest import DENY_TERM

OPENING = "Argus digest, 16–29 Nov 2026: 2 releases across 2 packages."
GOOD = OPENING + "\n\n• lens 0.13.0: caption facets for every image\n• atlas 0.4.0: painted coastlines"


class Chat:
    """An OpenAI-compatible server that answers with the queued replies, in order."""

    def __init__(self, *replies, status=200):
        self.replies, self.status, self.prompts = list(replies), status, []

    def request(self, method, url, body=None, headers=None):
        payload = json.loads(body)
        self.prompts.append(payload["messages"])
        text = self.replies.pop(0) if self.replies else ""
        return self.status, json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def x(audience=None):
    return TwitterPublisher().configure(Destination(audience=audience))


def test_rules_and_cleaning():
    req = Request("digest", "facts", OPENING)
    assert problems(GOOD, x(), "https://e.org/d/", req) == []
    bad = OPENING + " 🚀\n\n• a\n• b\n• c\n• d https://x.org #python"
    found = problems(bad, x(), "https://e.org/d/", req)
    assert any("emoji" in p for p in found) and any("link" in p for p in found)
    assert any("hashtags" in p for p in found) and any("4 items" in p for p in found)
    assert any("didn't start with" in p for p in problems("Hello", x(), "", req))
    assert any("too long" in p for p in problems(OPENING + " " + "word " * 60, x(), "https://e.org", req))
    assert problems(GOOD + " 🙂", x(Audience(emoji=True)), "", req) == []
    assert clean("<think>plan it</think>\n\"Hello\"") == "Hello"
    assert clean("• lens 0.12.0: `serve` binds **localhost**") == "• lens 0.12.0: serve binds localhost"
    assert clean("reasoning without an opening tag</think>Hi") == "Hi"
    assert any("empty" in p for p in problems(clean("<think>only</think>"), x(), "", req))


def test_one_retry_names_the_rule_then_falls_back():
    req = Request("digest", "facts", OPENING)
    chat = Chat(GOOD + " 🚀")  # emoji is removed without asking again
    backend = OpenAICompatible(chat, "http://192.168.0.9:11434/v1", "m")
    assert draft(backend, x(), req, "https://e.org", None) == GOOD and len(chat.prompts) == 1
    chat = Chat(GOOD + " see https://x.org", GOOD)  # a link can't be repaired: one retry naming it
    backend = OpenAICompatible(chat, "http://192.168.0.9:11434/v1", "m")
    assert draft(backend, x(), req, "https://e.org", None) == GOOD
    assert "included a link" in chat.prompts[1][1]["content"]
    five = OPENING + "\n\n" + "\n".join(f"• pkg{i} 0.{i}.0: a short change" for i in range(5))
    trimmed = draft(OpenAICompatible(Chat(five), "http://localhost/v1", "m"), x(), req, "https://e.org", None)
    assert trimmed.count("•") == 3 and trimmed.endswith("pkg2 0.2.0: a short change")
    long_items = OPENING + "\n\n" + "\n".join(f"• pkg{i} 0.{i}.0: " + "longword " * 9 for i in range(3))
    fitted = draft(OpenAICompatible(Chat(long_items), "http://localhost/v1", "m"), x(), req, "https://e.org", None)
    assert 1 <= fitted.count("•") < 3
    with pytest.raises(DraftingError, match="twice"):
        draft(OpenAICompatible(Chat("Hello #python", "Hi #python"), "http://localhost/v1", "m"), x(), req, "", None)
    with pytest.raises(DraftingError, match="HTTP 500"):
        draft(OpenAICompatible(Chat(status=500), "http://localhost/v1", "m"), x(), req, "", None)


def test_remote_backend_gets_scrubbed_facts_and_needs_a_gate(gate):
    class Remote(Backend):
        name, remote = "remote", True

        def generate(self, system, prompt):
            self.seen = prompt
            return "A plain sentence about the release."

    backend = Remote("m")
    facts = f"Built on {DENY_TERM} at /home/alice/x, ping bob@corp.test from 192.168.1.20"
    draft(backend, x(), Request("headline", facts), "", gate)
    assert DENY_TERM not in backend.seen and "/home/alice" not in backend.seen and "bob@corp.test" not in backend.seen
    assert "[name]" in backend.seen and "[email]" in backend.seen and "[address]" in backend.seen
    with pytest.raises(DraftingError, match="privacy gate unavailable"):
        draft(backend, x(), Request("headline", facts), "", None)


def test_backend_config_and_urls(tmp_path):
    settings = DraftingSettings(backend="openai_compatible")
    assert configured_backend(settings, {}, Chat())[1].startswith("assisted drafting is off: Set PIP_RELEASE_LLM_BASE_URL")
    env = {"PIP_RELEASE_LLM_BASE_URL": "http://192.168.0.9:11434/v1", "PIP_RELEASE_LLM_MODEL": "mistral-nemo"}
    assert backend_from(settings, env, Chat()).label == "openai_compatible:mistral-nemo"
    for url in ("http://example.com/v1", "ftp://192.168.0.9/v1", "http://user:pw@localhost/v1"):
        with pytest.raises(DraftingError):
            backend_from(settings, {**env, "PIP_RELEASE_LLM_BASE_URL": url}, Chat())
    assert backend_from(DraftingSettings(), env, Chat()) is None
    assert backend_from(DraftingSettings(backend="anthropic"), {}, Chat()).label == "anthropic:claude-opus-5-5"
    path = tmp_path / "c.yaml"
    for bad in ("drafting: {backend: gpt}\n", "drafting: {models: {summary: x}}\n", "drafting: {timeout: 1}\n",
                "drafting: {effort: extreme}\n"):
        path.write_text("pypi: {projects: [a]}\n" + bad, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)
    path.write_text("pypi: {projects: [a]}\ndrafting: {backend: openai_compatible, models: {digest: big}}\n", encoding="utf-8")
    assert load_config(path).drafting.model_for("digest") == "big"


def test_anthropic_request_shape_and_refusal(monkeypatch):
    import anthropic

    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            calls.append(kwargs)
            stop = "refusal" if "decline" in kwargs["messages"][0]["content"] else "end_turn"
            return types.SimpleNamespace(stop_reason=stop, content=[types.SimpleNamespace(type="text", text="Done.")])

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    backend = AnthropicBackend("claude-opus-5-5", "low", 30)
    assert backend.generate("sys", "facts") == "Done."
    sent = calls[0]
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"
    assert sent["betas"] == ["server-side-fallback-2026-07-01"] and sent["output_config"] == {"effort": "low"}
    assert "temperature" not in sent
    with pytest.raises(DraftingError, match="declined"):
        backend.generate("sys", "please decline")


def _digest(tmp_path):
    from pip_release.config import DigestSettings
    from datetime import date

    settings = DigestSettings(anchor=date(2026, 11, 16), site_url="https://example.org/updates/", site_dir=tmp_path)
    store = Store(tmp_path / "s.sqlite")
    for project, version, day in (("argus-lens", "0.13.0", 18), ("argus-atlas", "0.4.0", 19)):
        store.add_release(project=project, version=version, released_at=f"2026-11-{day}T00:00:00+00:00", summary="s",
                          notes="### Added\n- Something new\n", url=f"https://pypi.org/project/{project}/{version}/")
    return settings, store, store.create_digest("2026-11-16", "2026-11-29", "2026-11-30T00:00:00+00:00")


def test_digest_uses_the_model_and_falls_back_with_a_note(tmp_path, gate):
    settings, store, digest_id = _digest(tmp_path)
    backend = OpenAICompatible(Chat(GOOD, "#news", "#news"), "http://localhost/v1", "nemo")
    ids, _ = build_digest(store, settings, [TwitterPublisher(), PatreonPublisher()], digest_id, gate, backend)
    x_draft, patreon = store.get(ids[0]), store.get(ids[1])
    assert x_draft.drafted_by == "openai_compatible:nemo"
    assert x_draft.body == GOOD + "\n\nhttps://example.org/updates/2026-11-30-digest/"
    assert patreon.drafted_by == "template" and patreon.note.startswith("Template used: assisted draft broke the rules twice")

    store.update_text(ids[0], "", "My words", ("pending",))
    with pytest.raises(ValueError, match="edited by a person"):
        regenerate(store, settings, TwitterPublisher(), ids[0], backend, gate)
    backend.http.replies = [GOOD]
    regenerate(store, settings, TwitterPublisher(), ids[0], backend, gate, force=True)
    assert store.get(ids[0]).body.startswith(OPENING) and not store.get(ids[0]).edited
    store.close()
