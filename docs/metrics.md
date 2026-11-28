# Metrics

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

## Dashboard

`/dashboard` in the review UI (same sign-in) asks one question of the metrics: do announcements move adoption, and which platform and format does it best? It reads `metrics.sqlite` on this machine and nothing leaves it unless you export a CSV. The charts are inline SVG, with no CDN, in light and dark.

- **Timeline:** PyPI downloads, GitHub views, unique visitors, clones, new stars and site visits on one shared time axis, each with a 7-day mean. Grey lines mark releases; coloured ones mark posts (hover for the text, click to open it). Linear or log scale, for the suite or one package.
- **Followers** per platform.
- **GitHub referrers:** unique visitors by source (X, Bluesky, Mastodon, Reddit, Hacker News, search, your own site, other), stacked.
- **Broadcasts:** one row per post, with engagement at 24h and 7d and the change in downloads, views, stars and that platform's referrers. The change compares the 72 hours from the post with the median day of the 14 days before. These are associations, not effects; rows whose window overlaps another post are greyed out. Sort by any column; filter by platform, format, media, drafting backend, or herald vs logged by hand.
- **Platforms by week:** posts, engagement, referrer uniques and follower change. Weeks start on the digest anchor's weekday (Monday by default); change it in the filters.
- **Clips:** one row per clip in the [registry](media.md#clip-registry), in the same order as `argus-herald clips`: `redo` first, then `check`, `unknown` and the rest, and within each the best-performing first. Each row shows the clip's title, id, demo and format, its status, a [freshness](media.md#freshness) badge (open "What changed" for the commits, the UI files and the version move), a [performance](media.md#performance) badge per platform with its impressions or engagements, and the date of its last post, linked when the post has an https URL. A clip that is both `redo` and `strong` is marked "re-record first". Above the table: the demos with no current clip, and how many clips are in each status. The range and package filters don't apply to it.

Every table except Clips has a CSV link with the page's filters applied; `argus-herald clips export` covers the registry.

## Followers

`collect` also records audience size per platform per day in `account_metrics`:

```yaml
accounts:
  bluesky: dragonhound.dev          # public profile API, no login
  mastodon: "@name@mastodon.example"  # public account lookup, no login
  # youtube: UC…                    # defaults to sources.youtube; needs YOUTUBE_API_KEY
```

An account that doesn't exist yet is reported as skipped until it does. X has no free API for this, so enter its numbers by hand, for example from the analytics page each Sunday:

```bash
argus-herald import x-account --date 2026-11-22 --followers 196 --following 410 --posts 2580
argus-herald followers     # latest count per platform, with the change over 7 days
```

## Engagement per post

Each Bluesky and Mastodon post in the broadcast list (published by herald, or logged by hand with its URL) is read from the public API at +1h, +24h, +72h and +7d after it went out. The samples go to `post_metrics` as a time series of likes, reposts, replies and quotes. The watcher checks once an hour, and fetches only posts that have reached a checkpoint. A post deleted since is reported as skipped.

X has no free API for this, so import the analytics export instead. Per-post exports are matched to herald's broadcasts by post id. Posts made outside herald are stored too. An account overview export (one row per day) goes to `account_engagement`. When only the week's totals are at hand, enter them:

```bash
argus-herald import x-analytics ~/Downloads/account_analytics_content.csv
argus-herald import x-analytics ~/Downloads/account_overview_analytics.csv
argus-herald import x-week --end 2026-11-29 --impressions 9000 --engagements 310 --link-clicks 40 --profile-visits 55 --new-follows 16
```

Importing the same post again adds a new sample, so a weekly import draws the curve for X as well. `slots suggest` uses this data.

## Site traffic

With Cloudflare Web Analytics on the site, `collect` keeps daily page views and visits per host and path (`site_traffic`) and per referring host (`site_referrers`) for the last 7 days on each run. Subdomains that report under one site tag are kept apart by host. Set `CF_API_TOKEN` (a token with Account Analytics: read), `CF_ACCOUNT_ID` and `CF_SITE_TAG` (from the Web Analytics site's snippet) in `.env`. `argus-herald platforms` lists which metrics sources are set up.

## Posts made outside herald

Some posts are always made by hand: Reddit, Hacker News, YouTube uploads, newsletter and link-roundup submissions. Log them so they appear on the metrics timeline next to herald's own posts:

```bash
argus-herald log-broadcast --platform reddit --url https://www.reddit.com/r/gis/comments/… --kind demo --project argus-atlas
argus-herald log-broadcast --platform hn --url "https://news.ycombinator.com/item?id=…" --at 2027-01-05T15:00Z
argus-herald broadcasts                    # list
argus-herald log-broadcast --id 3 --subject "Show HN: atlas"   # edit
argus-herald broadcasts delete 3
```

`--platform` is any short name. `x` or `twitter` URLs are normalised like confirmed X posts. `--at` defaults to now, in UTC. The review UI has the same under **Logged posts**. Each `collect` rebuilds the broadcast events, so edits and deletions carry through.
