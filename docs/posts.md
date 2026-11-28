# Posts that aren't releases

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

## Teasers from argus-mint

`sources.mint` turns each teaser approved in [argus-mint](https://github.com/smk762/argus-mint) into a draft, with mint's PNG attached and its reviewed alt text:

```yaml
sources:
  mint:
    project: example-teasers   # optional: only this mint project's teasers
    platforms: [twitter]       # default: every enabled destination that takes images
```

Mint's address goes in `.env` as `ARGUS_MINT_URL`, never in the config (plain `http://` is accepted only for this machine or a private address). `ARGUS_MINT_URL` is also the suite-wide name for mint (`argus_cortex.peers`): when `.env` leaves it unset or empty, herald reads it from the Argus suite settings file, `~/.config/argus/suite.env` (mode 600; `python -m argus_cortex.settings --init` creates it), and holds it to the same rule. A value in `.env` wins, and `ARGUS_MINT_URL=off` there ignores the suite file's. Nothing else comes from that file: the poster token and the platform tokens are read from herald's own environment only. Herald pulls from mint and mint never calls herald, so it works from a laptop that is only sometimes on mint's network: while mint is out of reach the poll skips it.

- The first read baselines the source, so teasers already approved are not drafted.
- A teaser that has been superseded by a newer draft, or already has a post, is skipped.
- Drafts are keyed by mint's draft id (`mint:<id>`), so a teaser is never drafted twice.
- Only destinations that carry images get a draft (X and Patreon of the built-in ones).

When the post is live, herald tells mint: `argus-herald confirm <id> --url <post url>` (or the same in the review UI) sends the URL, the time and the sha256 of the image herald posted to mint's `POST /drafts/{id}/posted`. This needs `ARGUS_MINT_POSTER_TOKEN` in `.env`. Mint records only a post of the exact image it reviewed, so herald refuses to draft a teaser whose image it would have to change to clean. A confirm without a URL doesn't report yet; add the URL later and it does. A report that can't be delivered stays queued and is sent on the next poll.
