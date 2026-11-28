# CLAUDE.md — argus-herald

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md) and the guides under `docs/` (both imported by docs.dragonhound.dev; `tests/test_docs_publishable.py` holds them to its rules); this file is the orientation an agent needs to change code safely.

> Scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template) (`.copier-answers.yml`); `uvx copier update` pulls tooling changes.

## What this is

The Argus suite's release broadcaster, usable by any maintainer: it polls PyPI (and GitHub Releases, feeds and digests), drafts one announcement per platform, holds every draft for a person to approve, then publishes or hands off. It also records adoption and engagement metrics and shows them on a dashboard.

Herald uses argus-cortex for small shared helpers only (`hashing`, `timestamps`, `peers`) and mostly reads only public PyPI and GitHub data about the other Argus services. The one wire contract is with argus-mint (`mint.py`): `GET /posts/ready`, the draft image, and `POST /drafts/{id}/posted`. Mint's address is `ARGUS_MINT_URL` from `.env`, else from the suite file (`peer_url("mint")`); every token (platforms, `ARGUS_MINT_POSTER_TOKEN`) is read from herald's own environment only, never from the suite file.

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/dev/SUITE.md

## Layout

`src/argus_herald/`:

- `cli.py` — argparse CLI, entry point `argus-herald`; `watch` is the long-running loop.
- `config.py` — YAML/JSON config; secrets only ever come from the environment (`.env`).
- `store.py` / `models.py` / `workflow.py` — the SQLite draft queue and its state machine (`Store.transition`, the privacy gate in `enforce_gate`).
- `privacy.py` — the fail-closed gate: approve and publish are refused while the private deny-list is missing.
- `clips.py` — the demo clip registry (`clips` table; `draft_media.clip_id` and `logged_broadcasts.clip_id` record which posts used a clip).
- `freshness.py` — whether a recorded clip still matches its demo (GitHub compare + newest seen version); the last result is in `clip_freshness`, refreshed by `collect` and `clips refresh`.
- `performance.py` — how each clip's posts did per platform against the account's last 30 days (computed on read from `post_metrics`; `clip_performance` is the all-clips call for the dashboard).
- `mint.py` — `sources.mint`: approved argus-mint teasers become drafts with their PNG attached, and their posts are reported back (`mint_reports` queue, sent on confirm and on each poll). Herald pulls; mint never pushes.
- `platforms/` — publishers, plus the `argus_herald.platforms` entry-point group for plugins.
- `metrics.py`, `engagement.py`, `dashboard.py`, `charts.py` — collectors (separate `metrics.sqlite`) and the server-rendered SVG dashboard.
- `ui/` — Starlette + Jinja review UI on 127.0.0.1:8787.

## Commands

```bash
make install   # uv venv + editable install with the "dev,claude" extras
make test      # pytest --tb=short -q
make lint      # ruff check + format --check (pinned ruff 0.15.16)
make format    # ruff format + --fix
tests/bombadil/run.sh   # Bombadil UI properties (needs Node 22 and Chrome; see README)
```

Run one test: `uv run --no-sync pytest tests/test_x.py::test_name -q`.

## Conventions & gotchas

- **Versioning is git-tag-derived** (`hatch-vcs`). Never hand-edit a version; `src/argus_herald/_version.py` is generated (gitignored). Tag `vX.Y.Z` to release.
- **Tests never read the real deny-list.** `tests/conftest.py` points `ARGUS_HERALD_OPSEC_DENYLIST` at a fixture; keep it that way, and never print a deny-list's contents.
- **Nothing private in committed files:** no LAN addresses or hostnames (they go in `.env`), no home paths. The privacy gate checks drafts for the same things.
- Publishing is always behind a person's approval. Don't add a path that posts without one.
- The UI renders user text; templates autoescape, and charts escape `<title>` text and only link `https://` URLs.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never open a PR against `main` or push work to it directly: `main` must never be ahead of `dev`. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected and needs a PR, merge the release PR, then immediately merge `main` back into `dev` so the merge commit is not left on `main` alone.

## CI / release

CI runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow, plus the Bombadil `ui-properties` job. Release publishes to PyPI (OIDC trusted publishing) + GHCR on `v*` tags.
