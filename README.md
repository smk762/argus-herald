# pip-release

pip-release watches PyPI projects, drafts an announcement for each new version, and waits for a person to edit and approve it before anything is published. Posts that aren't releases (demos, build notes, videos) go through the same queue.

Public identities live in YAML or JSON. Tokens and webhook URLs live in `.env`. The first poll records versions that are already on PyPI and does not draft them. Later versions become one draft per enabled platform.

Discord and Telegram publish through their APIs. X and Patreon are handed to a person: an approved X draft becomes an **Open in X** link with the text filled in (no API call, so no per-post charge, and media can be attached by hand), and an approved Patreon draft is printed for the creator's editor, because Patreon's public API does not create posts. After posting, `pip-release confirm <id> --url <post url>` marks the draft published and keeps the URL for analytics.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip-release init --example dragonhound
```

`init` copies `examples/dragonhound.yaml` to `config.yaml` and `.env.example` to `.env`. That fixture tracks [dragonhound_dev](https://pypi.org/user/dragonhound_dev/) and names [dragonhound3](https://x.com/dragonhound3) and [AtlasRealms](https://www.patreon.com/c/AtlasRealms). Discord and Telegram are present and disabled until a channel exists. For any other maintainer, run `pip-release init` and edit `config.yaml`.

PyPI documents per-project RSS, not a feed of one user's projects, and the profile page often answers with a client challenge. `pip-release discover` prints project names when the profile is readable. Otherwise keep the explicit `pypi.projects` list.

`pip-release discover --check` looks for drift and exits 1 when it finds any, so it can run in CI. The watcher also runs it once a day and raises a desktop notice. It checks that:

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

`GITHUB_TOKEN` is optional and only raises the API rate limit. A project added to `pypi.projects` is baselined on the next poll (or right away with `pip-release baseline <name>`), so its old versions are never drafted.

## Review

Open the queue in a browser:

```bash
pip-release ui
```

That listens on `http://127.0.0.1:8787/`. Drafts are grouped by release. You can edit the text, approve or reject it, and publish from the same page. Saving an edit puts the draft back to pending. For X, Publish shows **Open in X** and **Copy text**. For Patreon, it shows the title and HTML to paste. After posting, paste the post's URL and press “I published this on …”. A post confirmed without a URL can have one added later from the same page.

Set `PIP_RELEASE_UI_TOKEN` when the UI is reachable beyond your own machine. Docker’s review service listens inside the container on all interfaces and publishes only `127.0.0.1:8787` on the host, so it requires that token. `PIP_RELEASE_UI_URL` is included in the optional review webhook and printed after `poll` when new drafts appear.

The same actions are available from the shell:

```bash
pip-release poll          # baseline, or draft what is new
pip-release drafts
pip-release show 1
pip-release edit 1        # $EDITOR; the draft returns to pending
pip-release approve 1
pip-release publish 1
pip-release publish --approved --dry-run
pip-release confirm 1 --url https://x.com/<user>/status/<id>   # after a manual post
pip-release baseline      # mark every current version seen, without drafting
pip-release reset --reject-pending [--before 2026-11-15] [--yes]
```

`baseline` does what the first poll does, on demand and for projects that are already tracked: every version on PyPI right now is marked seen, so the next `poll` drafts only what is released after it. `reset --reject-pending` rejects pending drafts with the note `stale backlog`. It shows the count per project and asks first unless `--yes` is given. Together they clear a backlog of old releases nobody wants announced.

`pip-release watch` repeats `poll` on `poll.interval_seconds` (at least 60, default 1800). A new draft can also be posted as JSON to `PIP_RELEASE_REVIEW_WEBHOOK`. When `notify-send` is available, the same event raises a desktop notification that stays in the GNOME notification list. Set `PIP_RELEASE_DESKTOP_NOTIFY=0` to skip it. While any draft is still pending, that alert is repeated every 24 hours.

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
pip-release digest                          # open window and recent digests
pip-release digest exclude argus-lens==0.12.1
pip-release digest rebuild 3
pip-release promote argus-lens==0.13.0 --site-page
```

Without a `digest:` block (or with `mode: per_release`), every release gets its own drafts as before.

## Metrics

`pip-release collect` snapshots adoption metrics into `metrics.sqlite`, next to the queue. They stay on this machine. The watcher runs it once a day. Every write is an upsert, so running it again does no harm.

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

### Posts made outside herald

Some posts are always made by hand: Reddit, Hacker News, YouTube uploads, newsletter and link-roundup submissions. Log them so they appear on the metrics timeline next to herald's own posts:

```bash
pip-release log-broadcast --platform reddit --url https://www.reddit.com/r/gis/comments/… --kind demo --project argus-atlas
pip-release log-broadcast --platform hn --url "https://news.ycombinator.com/item?id=…" --at 2027-01-05T15:00Z
pip-release broadcasts                    # list
pip-release log-broadcast --id 3 --subject "Show HN: atlas"   # edit
pip-release broadcasts delete 3
```

`--platform` is any short name. `x` or `twitter` URLs are normalised like confirmed X posts. `--at` defaults to now, in UTC. The review UI has the same under **Logged posts**. Each `collect` rebuilds the broadcast events, so edits and deletions carry through.

## Posts that aren't releases

Demos, build notes, new pages and videos go through the same queue: one pending draft per destination, the privacy gate, approve, then publish or hand off.

```bash
pip-release compose "Hex territories on real terrain." --kind demo --link https://example.org/realms
echo "How the curator scores a dataset." | pip-release compose - --kind note --subject "Curator's numbers"
pip-release approve --post post:3f2a…        # every draft of one post
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

`pip-release platforms` prints each audience.

## Media

Any draft can carry images, GIFs or short clips, and each one needs alt text. On a draft's review page, upload a file or pick one from the media folder (`.pip-release/media/` by default, or `media: {dir: …}`), and optionally attach it to every draft of the same post. From the shell:

```bash
pip-release media add 12 ~/clips/atlas-coast.gif --alt "A coastline painted over real terrain, labels fading in" --all-drafts
pip-release media list 12
pip-release media alt 7 "Better description"
pip-release compose "Coastlines, painted." --kind demo --media shot.png --alt "…"
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

The deny-list is a file outside the repository, one term per line, `#` comments allowed: `~/.config/pip-release/opsec-denylist.txt` by default, or the path in `PIP_RELEASE_OPSEC_DENYLIST`. Keep it `chmod 600`. If [ai-code-auditor](https://github.com/smk762/ai-code-auditor)'s list exists, its terms are enforced too. **Without the deny-list nothing can be approved or published.**

```yaml
privacy:
  allow_addresses: ["0x…"]       # public contracts that may appear in posts
  patterns:
    ticket: "INTERNAL-\\d+"
```

The review page highlights each hit. Edit the text, or accept one specific hit for that draft with a note saying why it is safe. From the shell:

```bash
pip-release check                 # pending and approved drafts
pip-release accept 12 'crypto-address:0x…' --note "public demo contract"
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
| `twitter` | none with `mode: manual` (default). With `mode: api`: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | Manual: an `https://x.com/intent/post?text=…` link for a person to post, then `pip-release confirm <id> --url <post url>`. API: `POST https://api.x.com/2/tweets` with OAuth 1.0a; a post that includes a link is billed at the URL rate on the X developer console. Both modes cap posts at 280 weighted characters, and a URL counts as 23. |
| `discord` | `DISCORD_WEBHOOK_URL` | Incoming webhook. The edited text is the message, up to 2000 characters. |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `sendMessage` as HTML. The bot must be allowed to post in the channel. |
| `patreon` | none | Prints title and HTML. `pip-release confirm <id> [--url <post url>]` records that the creator published it. |

`pip-release platforms` prints which destinations are enabled and which secret names are still empty. It does not print secret values.

Set `enabled: false` to keep a platform in the file without drafting for it. A platform omitted from `destinations` is not used. Unknown ids are an error.

## Add a platform

Implement the `Publisher` methods (`render`, `validate`, `missing_secrets`, `publish`) and register an entry point in group `pip_release.platforms`. Then add a `destinations.<id>` block. Built-in publishers live in `src/pip_release/platforms/__init__.py`.

Likely next publishers, in the order they are useful for a Python release: Mastodon, Bluesky, LinkedIn, Slack, Matrix, and an email or RSS handoff. Lemmy, Nostr, and Threads are further out. Mastodon and Bluesky already have maintained release actions elsewhere; this queue still has to own the PyPI poll and the approval step.

## Docker

Copy a config into place first (`pip-release init --example dragonhound` or `cp config.example.yaml config.yaml`), then:

```bash
docker compose up -d --build
docker compose run --rm watcher drafts
```

The review service is on `http://127.0.0.1:8787/` after `PIP_RELEASE_UI_TOKEN` is set in `.env`. The watcher runs as uid 10001 with a read-only root filesystem. Config is mounted read-only. The queue is the `pip-release-data` volume. Secrets come from `.env`.

## Tests

```bash
pytest
```
