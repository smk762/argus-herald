"""Assisted drafting: a model writes the first draft of a digest or headline post.

A person still edits and approves every word. The model writes only the post's text; herald
adds the link and hashtags itself, then checks the rules in code, because a model doesn't
reliably follow them: no emoji unless the audience allows them, no links or hashtags of its
own, at most three items, and the post must fit the platform without clipping. A broken rule
gets one retry naming it; after that the template is used instead.

Backends:
- ``openai_compatible``: a local server (Ollama's ``/v1``, llama.cpp, vLLM). Preferred, because
  draft inputs stay on the machine. ``ARGUS_HERALD_LLM_BASE_URL`` and ``ARGUS_HERALD_LLM_MODEL``
  live in ``.env``.
- ``anthropic``: Claude, through the official SDK (``pip install 'argus-herald[claude]'``).
  Input is scrubbed by the privacy gate first; without a gate nothing is sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from argus_herald.config import Audience, DraftingSettings
from argus_herald.http import Http, HttpClient, HttpError
from argus_herald.models import Post
from argus_herald.platforms import Publisher
from argus_herald.privacy import Gate

MAX_ITEMS = 3
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
_THINK = re.compile(r"(?is)<think>.*?</think>|^.*?</think>")
_EMOJI = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002b00-\U00002bff️]")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HASHTAG = re.compile(r"(?<![\w&])#\w+")
_ITEM = re.compile(r"(?m)^\s*(?:[•\-*]|\d+[.)])\s+")


class DraftingError(Exception):
    pass


@dataclass(frozen=True)
class Request:
    task: str  # digest | headline
    facts: str  # what happened, for the model
    opening: str = ""  # a line the post must start with (digest: period and counts)


class Backend:
    name = "template"
    remote = False

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    def generate(self, system: str, prompt: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class OpenAICompatible(Backend):
    name = "openai_compatible"

    def __init__(self, http: Http, base_url: str, model: str, api_key: str = "") -> None:
        super().__init__(model)
        self.http, self.base_url, self.api_key = http, base_url.rstrip("/"), api_key

    def generate(self, system: str, prompt: str) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 1200,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        try:
            status, raw = self.http.request("POST", url, json.dumps(body).encode(), headers)
        except (HttpError, OSError) as exc:
            raise DraftingError(f"{self.name} unreachable: {exc}") from exc
        if status != 200:
            raise DraftingError(f"{self.name} answered HTTP {status}")
        try:
            content = json.loads(raw.decode("utf-8"))["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError) as exc:
            raise DraftingError(f"{self.name} returned an unexpected response") from exc
        return content


class AnthropicBackend(Backend):
    name = "anthropic"
    remote = True

    def __init__(self, model: str, effort: str, timeout: int) -> None:
        super().__init__(model)
        self.effort, self.timeout = effort, timeout

    def generate(self, system: str, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise DraftingError("drafting.backend anthropic needs: pip install 'argus-herald[claude]'") from exc
        try:
            client = anthropic.Anthropic(timeout=float(self.timeout), max_retries=2)
            # On a policy decline the API re-runs the request on a fallback model in the same call.
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.effort},
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError as exc:
            raise DraftingError("Claude rejected the credentials (ANTHROPIC_API_KEY or `ant auth login`)") from exc
        except anthropic.RateLimitError as exc:
            raise DraftingError("Claude rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            raise DraftingError(f"Claude answered HTTP {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise DraftingError("Claude unreachable") from exc
        if response.stop_reason == "refusal":
            raise DraftingError("Claude declined to draft this post")
        return "".join(block.text for block in response.content if block.type == "text")


def backend_from(settings: DraftingSettings, env: Mapping[str, str], http: Http) -> Backend | None:
    """The configured backend, or None for templates. Raises DraftingError when misconfigured."""
    if settings.backend == "openai_compatible":
        base = (env.get("ARGUS_HERALD_LLM_BASE_URL") or "").strip()
        model = (env.get("ARGUS_HERALD_LLM_MODEL") or "").strip() or settings.model
        if not base or not model:
            raise DraftingError("Set ARGUS_HERALD_LLM_BASE_URL and ARGUS_HERALD_LLM_MODEL in .env")
        _check_local_url(base)
        client = LanHttpClient(timeout=settings.timeout) if isinstance(http, HttpClient) else http
        return OpenAICompatible(client, base, model, (env.get("ARGUS_HERALD_LLM_API_KEY") or "").strip())
    if settings.backend == "anthropic":
        return AnthropicBackend(settings.model or DEFAULT_CLAUDE_MODEL, settings.effort, settings.timeout)
    return None


def configured_backend(settings: DraftingSettings, env: Mapping[str, str], http: Http) -> tuple[Backend | None, str]:
    """(backend or None, a warning when assisted drafting is configured but can't run)."""
    try:
        return backend_from(settings, env, http), ""
    except DraftingError as exc:
        return None, f"assisted drafting is off: {exc}"


def _check_local_url(url: str) -> None:
    """https anywhere; plain http only to this machine or a private network address."""
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise DraftingError("The model URL must not include credentials")
    if parsed.scheme == "https":
        return
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host:
        if host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home.arpa")):
            return
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (ip.is_private or ip.is_loopback):
            return
    raise DraftingError("ARGUS_HERALD_LLM_BASE_URL must be https://, or http:// to this machine or the LAN")


class LanHttpClient(HttpClient):
    """HttpClient that also allows plain http to a local or private-network model server."""

    def request(self, method, url, body=None, headers=None):  # noqa: ANN001
        _check_local_url(url)
        from urllib.error import HTTPError, URLError
        from urllib.request import Request as UrlRequest

        from argus_herald.http import USER_AGENT

        merged = {"User-Agent": USER_AGENT, **(headers or {})}
        try:
            with self._opener.open(
                UrlRequest(url, data=body, headers=merged, method=method), timeout=self.timeout
            ) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()
        except (URLError, TimeoutError) as exc:
            raise HttpError(method, url, 0) from exc


# ---- prompting and checking ------------------------------------------------------------


def budget(publisher: Publisher, link: str) -> int:
    """Characters the model may use, leaving room for the link and hashtags."""
    audience = publisher.audience
    limit = getattr(publisher, "limit", 10000)
    room = limit
    if link and not (audience and audience.link_placement == "reply"):
        room -= 25
    if audience and audience.hashtags and publisher.max_hashtags:
        room -= 2 + sum(len(t) + 2 for t in audience.hashtags[: publisher.max_hashtags])
    return max(80, min(room, 600))


def system_prompt(publisher: Publisher, request: Request, chars: int) -> str:
    audience: Audience | None = publisher.audience
    lines = [
        f"You write short posts for {publisher.label} announcing releases of open-source software.",
        "Write only the post text, in plain language. Be specific; no hype words.",
    ]
    if audience and audience.description:
        lines.append(f"The people reading it: {audience.description}.")
    if audience and audience.tone:
        lines.append(f"Tone: {audience.tone}.")
    lines += [
        "Do not include any links, URLs or hashtags; they are added separately.",
        "Do not use emoji." if not (audience and audience.emoji) else "Emoji are fine, sparingly.",
        f"Keep it under {chars} characters.",
    ]
    if request.task == "digest":
        lines += [
            f"After the opening line, pick the 1 to {MAX_ITEMS} releases that matter most to these readers; fewer is fine.",
            "One line per release, in this form: • <package short name> <version>: <what changed, under 12 words>",
            "Mention each package at most once. No sub-points, no headings, nothing after the list.",
        ]
    else:
        lines.append("One or two sentences on what someone can now do with this release.")
    if request.opening:
        lines.append(f"Start with exactly this line: {request.opening}")
    return "\n".join(lines)


def problems(text: str, publisher: Publisher, link: str, request: Request) -> list[str]:
    """Rules the generated text breaks."""
    found = []
    audience = publisher.audience
    if not text.strip():
        found.append("it was empty")
    if not (audience and audience.emoji) and _EMOJI.search(text):
        found.append("it used emoji; use none")
    if _URL.search(text):
        found.append("it included a link; leave links out")
    if _HASHTAG.search(text):
        found.append("it included hashtags; leave them out")
    items = len(_ITEM.findall(text))
    if items > MAX_ITEMS:
        found.append(f"it listed {items} items; list at most {MAX_ITEMS}")
    if request.opening and not text.strip().startswith(request.opening):
        found.append(f"it didn't start with: {request.opening}")
    rendered = publisher.render_post(Post(kind=request.task, subject="", text=text.strip(), link=link))
    if text.strip() and text.strip() not in rendered.body:
        found.append(f"it was too long; keep it under {budget(publisher, link)} characters")
    return found


def clean(raw: str) -> str:
    text = _THINK.sub("", raw).strip()
    text = re.sub(r"^```\w*\n|\n```$", "", text).strip()
    # Social posts are plain text: backticks and bold markers would show literally.
    text = text.replace("`", "").replace("**", "")
    return text.strip('"').strip()


def repair(text: str, publisher: Publisher, link: str, request: Request) -> tuple[str, list[str]]:
    """Fix what can be fixed without rewriting: remove emoji, and for a digest drop whole items
    from the end while there are too many or the post doesn't fit. Returns (text, what was done)."""
    done: list[str] = []
    audience = publisher.audience
    if not (audience and audience.emoji) and _EMOJI.search(text):
        text = re.sub(r"[ \t]*" + _EMOJI.pattern + r"+", "", text)
        done.append("removed emoji")
    if request.task != "digest":
        return text, done
    lines = text.strip().splitlines()
    dropped = 0

    def too_long(candidate: str) -> bool:
        rendered = publisher.render_post(Post(kind="digest", subject="", text=candidate, link=link))
        return candidate not in rendered.body

    while True:
        joined = "\n".join(lines).strip()
        item_rows = [i for i, line in enumerate(lines) if _ITEM.match(line)]
        if len(item_rows) <= 1 or (len(item_rows) <= MAX_ITEMS and not too_long(joined)):
            break
        del lines[item_rows[-1] :]
        dropped += 1
    if dropped:
        done.append(f"dropped {dropped} item{'s' if dropped != 1 else ''} to fit")
    return "\n".join(lines).strip(), done


def draft(backend: Backend, publisher: Publisher, request: Request, link: str, gate: Gate | None) -> str:
    """Generate the post text, checked against the rules. Raises DraftingError to fall back."""
    facts = request.facts
    if backend.remote:
        if gate is None:
            raise DraftingError("privacy gate unavailable; nothing is sent to a remote model without it")
        facts = gate.scrub(facts)
    chars = budget(publisher, link)
    system = system_prompt(publisher, request, chars)
    text = clean(backend.generate(system, facts))
    broken = problems(text, publisher, link, request)
    if broken:
        text, _done = repair(text, publisher, link, request)
        broken = problems(text, publisher, link, request)
    if broken:
        retry = (
            facts
            + "\n\nYour last draft broke these rules: "
            + "; ".join(broken)
            + ". Write it again, following every rule."
        )
        text = clean(backend.generate(system, retry))
        broken = problems(text, publisher, link, request)
        if broken:
            text, _done = repair(text, publisher, link, request)
            broken = problems(text, publisher, link, request)
    if broken:
        raise DraftingError("assisted draft broke the rules twice: " + "; ".join(broken))
    return text
