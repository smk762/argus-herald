# Platforms, audiences and scheduling

Where posts go, who each destination is for, and when a post goes out. Back to the [README](../README.md).

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
