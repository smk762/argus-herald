# argus-herald

[![CI](https://github.com/smk762/argus-herald/actions/workflows/ci.yml/badge.svg)](https://github.com/smk762/argus-herald/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/argus-herald)](https://pypi.org/project/argus-herald/)
[![Python](https://img.shields.io/pypi/pyversions/argus-herald)](https://pypi.org/project/argus-herald/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

The [Argus suite](https://docs.dragonhound.dev)'s release broadcaster, and one any maintainer can run for their own packages. argus-herald watches PyPI projects, drafts an announcement for each new version, and waits for a person to edit and approve it before anything is published. Posts that aren't releases (demos, build notes, videos) go through the same queue.

Public identities live in YAML or JSON. Tokens and webhook URLs live in `.env`. The first poll records versions that are already on PyPI and does not draft them. Later versions become one draft per enabled platform.

Discord and Telegram publish through their APIs. X and Patreon are handed to a person: an approved X draft becomes an **Open in X** link with the text filled in (no API call, so no per-post charge, and media can be attached by hand), and an approved Patreon draft is printed for the creator's editor, because Patreon's public API does not create posts. After posting, `argus-herald confirm <id> --url <post url>` marks the draft published and keeps the URL for analytics.

## Upgrading from pip-release

This project used to be called pip-release. Until 0.3 the old names keep working and log a warning that names the new one:

| Before | Now |
|---|---|
| `pip-release` command | `argus-herald` |
| `PIP_RELEASE_*` variables | `ARGUS_HERALD_*` (the new name wins when both are set) |
| `.pip-release/` data directory | `.argus-herald/`, moved on first run unless `store.path` names the old one |
| `~/.config/pip-release/opsec-denylist.txt` | `~/.config/argus-herald/opsec-denylist.txt` (the old file is read until you move it) |
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

## Digests

A suite that releases often shouldn't post every version. With a `digest:` block, releases are collected into fixed windows instead of drafted one by one:

```yaml
digest:
  anchor: 2026-11-16          # first day of the first window (UTC)
  window_days: 14
  site_url: https://dragonhound.dev/updates/
  site_dir: /path/to/site/src/content/updates   # local checkout; herald writes here, never commits
  platforms: []               # default: every enabled destination
```

When a window closes (the watcher checks on every poll), herald creates one digest with every release seen before the close. A release seen after its window closed goes into the next one. For each digest it drafts:

- **a site page** `<close date>-digest.md` with front matter (`title`, `date`, `kind: digest`, `summary`, `period`, `releases` grouped by package, oldest version first) and `draft: true`, plus a short list of highlights for a person to rewrite into prose. The page passes the privacy gate first. Herald writes it only when no file is there, or when the file is still exactly what herald wrote, so your edits are never overwritten;
- **one post per destination**: the period, the count, up to three notable changes (as many as fit the platform without clipping), and the link to the page.

Each highlight is the first sentence of the release's first *Added* bullet, or of its first bullet, or its summary. A release is **suggested** for a headline post of its own when it's a major or minor bump, or a first release, and its notes add something. Patch releases and prereleases are never suggested. Suggestions are only badges: promoting is your call.

The **Digests** page in the review UI shows the open window and past digests. Drop a release from a digest, promote one to its own `headline` post (optionally with a `kind: release` site page), or **Rebuild** a digest after a change. Rebuild refreshes drafts nobody has edited, and the page if nobody has edited it. From the shell:

```bash
argus-herald digest                          # open window and recent digests
argus-herald digest exclude argus-lens==0.12.1
argus-herald digest rebuild 3
argus-herald promote argus-lens==0.13.0 --site-page
```

Without a `digest:` block (or with `mode: per_release`), every release gets its own drafts as before.

## Metrics

`argus-herald collect` snapshots adoption metrics into `metrics.sqlite`, next to the queue. They stay on this machine. The watcher runs it once a day. Every write is an upsert, so running it again does no harm.

| Table | From | Needs |
| --- | --- | --- |
| `gh_traffic` (daily views, uniques, clones) | GitHub traffic API | `GITHUB_TOKEN` |
| `gh_referrers`, `gh_paths` (snapshot per day) | GitHub traffic API | `GITHUB_TOKEN` |
| `gh_star_counts` (stars per repo per day) | GitHub repo list | nothing |
| `gh_stars` (when each star was given; the stargazer is stored only as a hash) | GitHub stargazers API | `GITHUB_TOKEN` |
| `pypi_downloads` (daily, without mirrors, last 180 days) | pypistats.org | nothing |
| `events` (release times, and published posts with their URL) | PyPI JSON API and the review queue | nothing |

Repos are the public, non-fork repos of `pypi.discover.github_owner` that start with `pypi.discover.prefix`. **GitHub keeps traffic for 14 days only**, so whatever isn't collected within that time is gone.

The traffic endpoints only answer to an account with write access to the repo. A fine-grained token limited to those repos, with **Metadata: read** and **Administration: read**, works for traffic. GitHub is limiting stargazer lists to admins and collaborators, and with such a token they answer 403. That is reported as skipped, not as an error, and `gh_star_counts` still tracks stars per day. `--since 2026-11-01` keeps only PyPI download days from that date.

### Dashboard

`/dashboard` in the review UI (same sign-in) asks one question of the metrics: do announcements move adoption, and which platform and format does it best? It reads `metrics.sqlite` on this machine and nothing leaves it unless you export a CSV. The charts are inline SVG, with no CDN, in light and dark.

- **Timeline:** PyPI downloads, GitHub views, unique visitors, clones, new stars and site visits on one shared time axis, each with a 7-day mean. Grey lines mark releases; coloured ones mark posts (hover for the text, click to open it). Linear or log scale, for the suite or one package.
- **Followers** per platform.
- **GitHub referrers:** unique visitors by source (X, Bluesky, Mastodon, Reddit, Hacker News, search, your own site, other), stacked.
- **Broadcasts:** one row per post, with engagement at 24h and 7d and the change in downloads, views, stars and that platform's referrers. The change compares the 72 hours from the post with the median day of the 14 days before. These are associations, not effects; rows whose window overlaps another post are greyed out. Sort by any column; filter by platform, format, media, drafting backend, or herald vs logged by hand.
- **Platforms by week:** posts, engagement, referrer uniques and follower change. Weeks start on the digest anchor's weekday (Monday by default); change it in the filters.

Every table has a CSV link with the page's filters applied.

### Followers

`collect` also records audience size per platform per day in `account_metrics`:

```yaml
accounts:
  bluesky: dragonhound.dev          # public profile API, no login
  mastodon: "@name@instance.social" # public account lookup, no login
  # youtube: UC…                    # defaults to sources.youtube; needs YOUTUBE_API_KEY
```

An account that doesn't exist yet is reported as skipped until it does. X has no free API for this, so enter its numbers by hand, for example from the analytics page each Sunday:

```bash
argus-herald import x-account --date 2026-11-22 --followers 196 --following 410 --posts 2580
argus-herald followers     # latest count per platform, with the change over 7 days
```

### Engagement per post

Each Bluesky and Mastodon post in the broadcast list (published by herald, or logged by hand with its URL) is read from the public API at +1h, +24h, +72h and +7d after it went out. The samples go to `post_metrics` as a time series of likes, reposts, replies and quotes. The watcher checks once an hour, and fetches only posts that have reached a checkpoint. A post deleted since is reported as skipped.

X has no free API for this, so import the analytics export instead. Per-post exports are matched to herald's broadcasts by post id. Posts made outside herald are stored too. An account overview export (one row per day) goes to `account_engagement`. When only the week's totals are at hand, enter them:

```bash
argus-herald import x-analytics ~/Downloads/account_analytics_content.csv
argus-herald import x-analytics ~/Downloads/account_overview_analytics.csv
argus-herald import x-week --end 2026-11-29 --impressions 9000 --engagements 310 --link-clicks 40 --profile-visits 55 --new-follows 16
```

Importing the same post again adds a new sample, so a weekly import draws the curve for X as well. `slots suggest` uses this data.

### Site traffic

With Cloudflare Web Analytics on the site, `collect` keeps daily page views and visits per path (`site_traffic`) and per referring host (`site_referrers`) for the last 7 days on each run. Set `CF_API_TOKEN` (a token with Account Analytics: read), `CF_ACCOUNT_ID` and `CF_SITE_TAG` (from the Web Analytics site's snippet) in `.env`. `argus-herald platforms` lists which metrics sources are set up.

### Posts made outside herald

Some posts are always made by hand: Reddit, Hacker News, YouTube uploads, newsletter and link-roundup submissions. Log them so they appear on the metrics timeline next to herald's own posts:

```bash
argus-herald log-broadcast --platform reddit --url https://www.reddit.com/r/gis/comments/… --kind demo --project argus-atlas
argus-herald log-broadcast --platform hn --url "https://news.ycombinator.com/item?id=…" --at 2027-01-05T15:00Z
argus-herald broadcasts                    # list
argus-herald log-broadcast --id 3 --subject "Show HN: atlas"   # edit
argus-herald broadcasts delete 3
```

`--platform` is any short name. `x` or `twitter` URLs are normalised like confirmed X posts. `--at` defaults to now, in UTC. The review UI has the same under **Logged posts**. Each `collect` rebuilds the broadcast events, so edits and deletions carry through.

## Assisted drafting

A model can write the first draft of each digest and headline post: it picks the changes that matter to each destination's audience and words them in its tone. A person still edits and approves every word.

```yaml
drafting:
  backend: openai_compatible     # template (default) | openai_compatible | anthropic
  models: {headline: other-model}  # optional, per task
```

- **`openai_compatible`** is a local server (Ollama's `/v1`, llama.cpp, vLLM), so draft inputs stay on your network. Put `ARGUS_HERALD_LLM_BASE_URL` (for example `http://<your-ollama-host>:11434/v1`; plain `http://` is accepted only for this machine or a private address) and `ARGUS_HERALD_LLM_MODEL` in `.env`, never in the committed config. `llama3.1:8b-instruct` did best in testing.
- **`anthropic`** uses Claude through the official SDK: `pip install 'argus-herald[claude]'`, with credentials from `ANTHROPIC_API_KEY` or `ant auth login`. The default model is `claude-opus-5-5` at `effort: low`, with the server-side refusal fallback on. The privacy gate scrubs everything sent (deny-list terms, emails, private addresses, paths). With no gate, nothing is sent.

The model writes only the text, and herald adds the link and hashtags. A model doesn't reliably follow rules, so herald checks them in code:

- no emoji (unless the audience sets `emoji: true`), no links or hashtags of its own;
- at most three items, and the post must fit the platform without clipping.

Emoji, and extra or overlong items at the end, are removed outright. Nothing is cut mid-sentence. Anything else gets one retry naming the broken rule, and after that the template draft is used, with a note on the draft saying why. Markdown backticks are removed, and reasoning output (`<think>…</think>`) is stripped.

Each draft records who wrote it ("Drafted by openai_compatible:llama3.1…"). **Regenerate** on a digest or headline draft writes it again, but only replaces your edits if you tick *replace my edits* (`argus-herald regenerate <id> [--force]`). Digest rebuilds and promotions use the model too.

## Posts that aren't releases

Demos, build notes, new pages and videos go through the same queue: one pending draft per destination, the privacy gate, approve, then publish or hand off.

```bash
argus-herald compose "Hex territories on real terrain." --kind demo --link https://example.org/realms
echo "How the curator scores a dataset." | argus-herald compose - --kind note --subject "Curator's numbers"
argus-herald approve --post post:3f2a…        # every draft of one post
```

The review UI has the same form under **New post**. `--kind` is one of `demo`, `note`, `video`, `page`. Each platform's draft is fitted to its own limit and keeps the link.

Two feeds can create posts on their own. Each is baselined on its first read, like PyPI, so nothing already in it is announced:

```yaml
sources:
  site:
    feed: https://dragonhound.dev/updates.xml   # Atom; entries whose category is listed become posts
    categories: [note]                         # build notes ("digest" and "release" are also accepted)
    platforms: [twitter]                       # default: every enabled destination
  youtube:
    channel_id: UCxZha6M0r90kFIGkFvKRvcw        # a new video, Shorts included, becomes a post
    platforms: [twitter]
```

Feeds are read on every `poll`, so the watcher picks them up too. Drafts from a feed are keyed by the item's id, so a feed entry is never drafted twice.

## Audiences

Each destination can describe who follows it and what they should get:

```yaml
destinations:
  twitter:
    audience:
      description: Crypto and AI-image people
      tone: plain and specific, visual first
      hashtags: [gis]               # appended while they fit; X takes at most 2
      link_placement: reply         # body (default) or reply
  patreon:
    audience:
      packages: [argus-atlas, argus-realms]   # or {include: [...], exclude: [...]}, globs allowed
```

- **Packages:** a destination only gets digests, headline posts and per-release drafts about packages it wants. A digest window with nothing for that audience produces no draft there, and a mixed digest is counted and summarised for that audience's packages only. Posts without a package, such as videos and build notes, go everywhere.
- **`link_placement: reply`:** keeps the post visual-first. The link moves into the draft's **Reply** field. On X, once you confirm the post's URL, herald offers **Open reply in X**, a reply intent under your post with the link filled in. Bluesky and Mastodon will post the reply themselves. Only those three can use `reply`.
- **`description` and `tone`:** shown on each draft for the reviewer, and passed to assisted drafting when that lands.

`argus-herald platforms` prints each audience.

## Scheduling

A destination with a schedule doesn't post the moment you approve. The draft waits for the next free slot:

```yaml
destinations:
  twitter:
    schedule:
      slots: [Tue 08:00, Fri 08:00]   # UTC
      min_gap_hours: 24               # distance from this destination's other posts
      offset_minutes: 0               # shift from the post's time on other platforms
      grace_hours: 6
```

- On approve, a draft gets the next slot that's at least `min_gap_hours` from that destination's other scheduled or published posts. Drafts of the same post go out together: once one is scheduled, the others take its time plus their own `offset_minutes`.
- The watcher wakes up for each scheduled draft. API platforms post. X and Patreon are handed off with a desktop notice ("Time to post"), and the draft page has the Open/Copy buttons ready.
- If the watcher wasn't running and a slot passed more than `grace_hours` ago, nothing is posted late: you get a notice, and the draft stays approved without a time.
- Editing an approved draft returns it to pending and releases its slot.
- On the draft page, pick another free slot, enter a time, unschedule, or press **Publish** to post now. From the shell:

```bash
argus-herald approve 12                 # also schedules it
argus-herald approve 12 --no-schedule   # goes out when you publish it
argus-herald schedule 12 --at 2026-12-01T08:00Z   # or --next, or --clear
argus-herald scheduled
argus-herald slots suggest              # engagement by weekday and hour, once post metrics exist
```

`slots suggest` ranks weekday and time-of-day buckets per platform by engagement, and suggests a slot once a platform has at least 6 posts. It only suggests and never changes the config.

## Media

Any draft can carry images, GIFs or short clips, and each one needs alt text. On a draft's review page, upload a file or pick one from the media folder (`.argus-herald/media/` by default, or `media: {dir: …}`), and optionally attach it to every draft of the same post. From the shell:

```bash
argus-herald media add 12 ~/clips/atlas-coast.gif --alt "A coastline painted over real terrain, labels fading in" --all-drafts
argus-herald media list 12
argus-herald media alt 7 "Better description"
argus-herald compose "Coastlines, painted." --kind demo --media shot.png --alt "…"
```

**Metadata never leaves the machine.** Each file gets a cleaned copy, and only that copy is posted or handed off:

- **JPEG, PNG and WebP:** the EXIF rotation is applied, then EXIF (including GPS), XMP, IPTC, comments and text chunks are dropped. The colour profile is kept.
- **GIF:** frames and timing are kept, and comments are dropped.
- **MP4, MOV and WebM:** `ffmpeg -map_metadata -1 -c copy`. Without ffmpeg, a clip is attached but marked unstripped, and approve refuses it.

The review page says what was removed (for example "EXIF with GPS location, comment").

Approve is refused while any attachment lacks alt text, when the privacy gate flags alt text, or when the attachments don't fit the platform:

- **X:** 4 images, or 1 GIF or video; images up to 5 MB, GIFs up to 15 MB.
- **Patreon:** more generous limits.
- Platforms that can't attach media yet refuse drafts that carry it.

For X and Patreon, the handoff lists the cleaned files to download and attach, each with its alt text ready to copy.

## Privacy gate

Every draft passes a privacy gate on approve and again on publish. A hit refuses the draft; text is never rewritten. It catches:

- private and link-local IPs, home and mount paths, email addresses (except `example.*`, `*.invalid` and GitHub noreply), and LAN hostnames (`.lan`, `.local`, `.internal`, …);
- crypto addresses (EVM, Base58Check with a valid checksum, bech32, ENS names), except those in `privacy.allow_addresses`;
- timezone offsets attached to a timestamp, as in a pasted log or `git log` line (`+00:00` and `Z` are fine);
- any term in your private deny-list, and any regex in `privacy.patterns`.

The deny-list is a file outside the repository, one term per line, `#` comments allowed: `~/.config/argus-herald/opsec-denylist.txt` by default, or the path in `ARGUS_HERALD_OPSEC_DENYLIST`. Keep it `chmod 600`. If [ai-code-auditor](https://github.com/smk762/ai-code-auditor)'s list exists, its terms are enforced too. **Without the deny-list nothing can be approved or published.**

```yaml
privacy:
  allow_addresses: ["0x…"]       # public contracts that may appear in posts
  patterns:
    ticket: "INTERNAL-\\d+"
```

The review page highlights each hit. Edit the text, or accept one specific hit for that draft with a note saying why it is safe. From the shell:

```bash
argus-herald check                 # pending and approved drafts
argus-herald accept 12 'crypto-address:0x…' --note "public demo contract"
```

## Start at boot

On a machine with systemd, install user services for the watcher and the review UI:

```bash
contrib/install-user-services.sh
```

They start at login, and at boot as well when lingering is enabled for your user. The script tries `loginctl enable-linger`. The review page stays on `http://127.0.0.1:8787/`. Notifications are sent on the session bus, so they show up after you are logged into the desktop.

Exit status is 1 when a project could not be read. Drafts that fail to publish stay `failed` and can be published again.

## Platforms

| Id | Needs in `.env` | Publish |
| --- | --- | --- |
| `twitter` | none with `mode: manual` (default). With `mode: api`: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | Manual: an `https://x.com/intent/post?text=…` link for a person to post, then `argus-herald confirm <id> --url <post url>`. API: `POST https://api.x.com/2/tweets` with OAuth 1.0a; a post that includes a link is billed at the URL rate on the X developer console. Both modes cap posts at 280 weighted characters, and a URL counts as 23. |
| `discord` | `DISCORD_WEBHOOK_URL` | Incoming webhook. The edited text is the message, up to 2000 characters. |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `sendMessage` as HTML. The bot must be allowed to post in the channel. |
| `patreon` | none | Prints title and HTML. `argus-herald confirm <id> [--url <post url>]` records that the creator published it. |

`argus-herald platforms` prints which destinations are enabled and which secret names are still empty. It does not print secret values.

Set `enabled: false` to keep a platform in the file without drafting for it. A platform omitted from `destinations` is not used. Unknown ids are an error.

## Add a platform

Implement the `Publisher` methods (`render`, `validate`, `missing_secrets`, `publish`) and register an entry point in group `argus_herald.platforms`. Then add a `destinations.<id>` block. Built-in publishers live in `src/argus_herald/platforms/__init__.py`.

Likely next publishers, in the order they are useful for a Python release: Mastodon, Bluesky, LinkedIn, Slack, Matrix, and an email or RSS handoff. Lemmy, Nostr, and Threads are further out. Mastodon and Bluesky already have maintained release actions elsewhere; this queue still has to own the PyPI poll and the approval step.

## Docker

Copy a config into place first (`argus-herald init --example dragonhound` or `cp config.example.yaml config.yaml`), then:

```bash
docker compose up -d --build
docker compose run --rm watcher drafts
```

The review service is on `http://127.0.0.1:8787/` after `ARGUS_HERALD_UI_TOKEN` is set in `.env`. The watcher runs as uid 10001 with a read-only root filesystem. Config is mounted read-only. The queue is the volume compose created before the rename, `pip-release_pip-release-data`, so existing drafts carry over. Release images are published as `ghcr.io/smk762/argus-herald`. Secrets come from `.env`.

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
- No 5xx, uncaught exceptions or console errors; WCAG AA text contrast; the chosen light or dark theme is the one shown.

```bash
cd tests/bombadil && npm install
CHROME=/path/to/chrome TIME_LIMIT=60s ./run.sh     # light, dark and token-locked runs
npx bombadil browser inspect out/light             # step through a run
```

`serve.py` starts the UI on a fresh temporary store with seeded drafts in every state, a fixture deny-list and fake publishers (nothing is posted), on a port the OS picks. CI runs both suites on every pull request.
