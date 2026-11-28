# Digests and assisted drafting

Collecting releases into fixed windows, and letting a model write the first draft. Back to the [README](../README.md).

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
