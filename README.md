<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/smk762/argus-cortex/dev/brand/packages/herald/argus-herald-lockup-dark.svg">
  <img alt="argus herald" src="https://raw.githubusercontent.com/smk762/argus-cortex/dev/brand/packages/herald/argus-herald-lockup-light.svg" height="52">
</picture>

# argus-herald

[![CI](https://github.com/smk762/argus-herald/actions/workflows/ci.yml/badge.svg)](https://github.com/smk762/argus-herald/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/argus-herald)](https://pypi.org/project/argus-herald/)
[![Python](https://img.shields.io/pypi/pyversions/argus-herald)](https://pypi.org/project/argus-herald/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

The [Argus suite](https://docs.dragonhound.dev)'s release broadcaster, and one any maintainer can run for their own packages. argus-herald watches PyPI projects, drafts an announcement for each new version, and waits for a person to edit and approve it before anything is published. Posts that aren't releases (demos, build notes, videos) go through the same queue.

Public identities live in YAML or JSON. Tokens and webhook URLs live in `.env`. The first poll records versions that are already on PyPI and does not draft them. Later versions become one draft per enabled platform.

Discord and Telegram publish through their APIs. X and Patreon are handed to a person: an approved X draft becomes an **Open in X** link with the text filled in (no API call, so no per-post charge, and media can be attached by hand), and an approved Patreon draft is printed for the creator's editor, because Patreon's public API does not create posts. After posting, `argus-herald confirm <id> --url <post url>` marks the draft published and keeps the URL for analytics.

## Upgrading from pip-release

This project used to be called pip-release. The old names kept working, with a warning, through 0.3. They are gone now, so rename anything that still uses them:

| Before | Now |
|---|---|
| `pip-release` command | `argus-herald` |
| `PIP_RELEASE_*` variables | `ARGUS_HERALD_*` |
| `.pip-release/` data directory | `.argus-herald/`: move the directory yourself, or point `store.path` at the old one |
| `~/.config/pip-release/opsec-denylist.txt` | `~/.config/argus-herald/opsec-denylist.txt`: move the file, since approve and publish are refused without it |
| `pip-release-watch` / `pip-release-ui` user services | `argus-herald-watch` / `argus-herald-ui`: rerun `contrib/install-user-services.sh` |
| `pip_release.platforms` entry points, `import pip_release` | `argus_herald.platforms`, `import argus_herald` |

The review UI's session cookie changed name too, so you sign in once more. Docker compose keeps using the volume it created under the old project name.

## Setup

```bash
pip install argus-herald            # or: uv tool install argus-herald
argus-herald init
```

From a checkout, `make install` creates `.venv` with the test extras (it needs [uv](https://docs.astral.sh/uv/)). `make test` and `make lint` run what CI runs. `argus-herald init --example dragonhound` starts from the suite's own config instead of a blank one.

`init --example dragonhound` copies `examples/dragonhound.yaml` to `config.yaml` and `.env.example` to `.env`. That fixture tracks [dragonhound_dev](https://pypi.org/user/dragonhound_dev/) and names [dragonhound3](https://x.com/dragonhound3) and [AtlasRealms](https://www.patreon.com/c/AtlasRealms). Discord and Telegram are present and disabled until a channel exists. For any other maintainer, run `argus-herald init` and edit `config.yaml`.

PyPI documents per-project RSS, not a feed of one user's projects, and the profile page often answers with a client challenge. `argus-herald discover` prints project names when the profile is readable. Otherwise keep the explicit `pypi.projects` list.

`argus-herald discover --check` looks for drift and exits 1 when it finds any, so it can run in CI. The watcher also runs it once a day and raises a desktop notice. It checks that:

- every configured name exists on PyPI;
- the PyPI profile, when it's readable, lists nothing that isn't configured;
- every public, non-fork repo of `pypi.discover.github_owner` whose name starts with `pypi.discover.prefix` and declares a `[project] name` in its `pyproject.toml` that is on PyPI is configured. PyPI can't list projects by prefix, so GitHub is how new packages are found.

```yaml
pypi:
  projects: [argus-atlas, …]
  discover:
    github_owner: smk762
    prefix: argus-
```

`GITHUB_TOKEN` is optional and only raises the API rate limit. A project added to `pypi.projects` is baselined on the next poll (or right away with `argus-herald baseline <name>`), so its old versions are never drafted.

## Review

Open the queue in a browser:

```bash
argus-herald ui
```

That listens on `http://127.0.0.1:8787/`. Drafts are grouped by release. You can edit the text, approve or reject it, and publish from the same page. Saving an edit puts the draft back to pending. For X, Publish shows **Open in X** and **Copy text**. For Patreon, it shows the title and HTML to paste. After posting, paste the post's URL and press “I published this on …”. A post confirmed without a URL can have one added later from the same page.

Set `ARGUS_HERALD_UI_TOKEN` when the UI is reachable beyond your own machine. Docker’s review service listens inside the container on all interfaces and publishes only `127.0.0.1:8787` on the host, so it requires that token. `ARGUS_HERALD_UI_URL` is included in the optional review webhook and printed after `poll` when new drafts appear.

The same actions are available from the shell:

```bash
argus-herald poll          # baseline, or draft what is new
argus-herald drafts
argus-herald show 1
argus-herald edit 1        # $EDITOR; the draft returns to pending
argus-herald approve 1
argus-herald publish 1
argus-herald publish --approved --dry-run
argus-herald confirm 1 --url https://x.com/<user>/status/<id>   # after a manual post
argus-herald baseline      # mark every current version seen, without drafting
argus-herald reset --reject-pending [--before 2026-11-15] [--yes]
```

`baseline` does what the first poll does, on demand and for projects that are already tracked: every version on PyPI right now is marked seen, so the next `poll` drafts only what is released after it. `reset --reject-pending` rejects pending drafts with the note `stale backlog`. It shows the count per project and asks first unless `--yes` is given. Together they clear a backlog of old releases nobody wants announced.

`argus-herald watch` repeats `poll` on `poll.interval_seconds` (at least 60, default 1800). A new draft can also be posted as JSON to `ARGUS_HERALD_REVIEW_WEBHOOK`. When `notify-send` is available, the same event raises a desktop notification that stays in the GNOME notification list. Set `ARGUS_HERALD_DESKTOP_NOTIFY=0` to skip it. While any draft is still pending, that alert is repeated every 24 hours.

## GitHub Releases

Repos that ship without PyPI (a web UI, a demo) can still be announced. Their GitHub releases are a second source:

```yaml
github:
  owner: smk762
  prefix: argus-              # every public, non-fork repo starting with this; or repos: [a, b]
  include_prereleases: true   # collected into digests, never drafted on their own
```

Each repo is baselined on its first read. With `GITHUB_TOKEN` the REST API is used. Without it, the public `releases.atom` feed is read, which never lists drafts. Tags must be versions (`v0.4.0` becomes `0.4.0`), so asset-only tags like `stock-heads` are skipped. A version released on both PyPI and GitHub is kept once, under PyPI, with the GitHub release notes, whichever was seen first. In a digest, GitHub-only releases are listed with `source: github`.

## Docs

| Guide | Covers |
| --- | --- |
| [docs/digests.md](docs/digests.md) | Collecting releases into fortnightly digests, headline posts, and model-assisted first drafts |
| [docs/posts.md](docs/posts.md) | Posts that aren't releases: `compose`, the site and YouTube feeds, teasers from argus-mint |
| [docs/channels.md](docs/channels.md) | The platforms and what each needs, adding one, audiences, and scheduled slots |
| [docs/media.md](docs/media.md) | Attachments, metadata stripping, alt text, and the clip registry with its freshness and performance ratings |
| [docs/metrics.md](docs/metrics.md) | What `collect` records, the dashboard, followers, engagement, site traffic, hand-logged posts |
| [docs/privacy.md](docs/privacy.md) | The privacy gate every draft passes, and the deny-list it needs |

## Start at boot

On a machine with systemd, install user services for the watcher and the review UI:

```bash
contrib/install-user-services.sh
```

They start at login, and at boot as well when lingering is enabled for your user. The script tries `loginctl enable-linger`. The review page stays on `http://127.0.0.1:8787/`. Notifications are sent on the session bus, so they show up after you are logged into the desktop.

Exit status is 1 when a project could not be read. Drafts that fail to publish stay `failed` and can be published again.

## Docker

Copy a config into place first (`argus-herald init --example dragonhound` or `cp config.example.yaml config.yaml`), then:

```bash
docker compose up -d --build
docker compose run --rm watcher drafts
```

The review service is on `http://127.0.0.1:8787/` after `ARGUS_HERALD_UI_TOKEN` is set in `.env`. The watcher runs as uid 10001 with a read-only root filesystem. Config is mounted read-only. The queue is the volume compose created before the rename, `pip-release_pip-release-data`, so existing drafts carry over. Release images are published as `ghcr.io/smk762/argus-herald`. Secrets come from `.env`.

To let the containers read the suite settings file, add [`compose.suite.yaml`](compose.suite.yaml) (`COMPOSE_FILE=compose.yaml:compose.suite.yaml` in `.env`). It mounts `${ARGUS_SUITE_ENV:-~/.config/argus/suite.env}` read-only at `/etc/argus/suite.env`; it is a separate file because a host without a suite file could not start with the mount in place. Known limit ([argus-cortex#218](https://github.com/smk762/argus-cortex/issues/218)): the containers run as uid 10001, which cannot open a mode-600 file it does not own, so today the mounted file has to be root-owned; otherwise it is ignored with a warning, and `ARGUS_MINT_URL` belongs in `.env`.

Herald is not labelled for the suite directory (no `argus.peer` label), because its review UI is published on loopback only and has no health route.

## Tests

```bash
make test    # pytest
make lint    # ruff check and format --check, pinned to the suite's ruff
```

CI runs the suite's shared [`argus-ci`](https://github.com/smk762/argus-ci) workflow (ruff, pytest on Python 3.11 and 3.12, build) plus the Bombadil job below. Tooling comes from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template): `uvx copier update` pulls its changes.

### Releasing

The version comes from the git tag (`hatch-vcs`): tagging `vX.Y.Z` publishes the wheel to PyPI through trusted publishing and the image to GHCR. There is no version string to edit.

### UI properties (Bombadil)

[Bombadil](https://github.com/antithesishq/bombadil) explores the review UI on its own (clicks, typing, Back, jumps to any route, forms forced past disabled buttons) and checks properties in every state it reaches:

- Approve and Publish are never enabled for a draft with an unresolved privacy hit or an attachment without alt text, and no forced submission gets such a draft approved.
- Publish and scheduling appear only for approved drafts (Publish also retries a failed one); Open in X only once an X draft is handed off.
- Saving an edit returns the draft to pending.
- With `ARGUS_HERALD_UI_TOKEN` set, only the sign-in page is reachable, and it shows nothing from the queue.
- The dashboard's Clips panel: every freshness and performance badge says its state in words, rows run `redo` first, a clip that is both `redo` and `strong` (and only such a clip) is marked "re-record first", and last-post links are https.
- No 5xx, uncaught exceptions or console errors; WCAG AA text contrast; the chosen light or dark theme is the one shown.

```bash
cd tests/bombadil && npm install
CHROME=/path/to/chrome TIME_LIMIT=60s ./run.sh     # light, dark and token-locked runs
npx bombadil browser inspect out/light             # step through a run
```

`serve.py` starts the UI on a fresh temporary store with seeded drafts in every state, a fixture deny-list and fake publishers (nothing is posted), on a port the OS picks. CI runs both suites on every pull request.

## Licence

The code is MIT ([LICENSE](LICENSE)). The argus-herald mark, its favicons and the lockup are Argus brand assets from [argus-cortex `brand/`](https://github.com/smk762/argus-cortex/tree/dev/brand), not covered by the MIT licence: see [its terms](https://github.com/smk762/argus-cortex/blob/dev/brand/LICENSE).
