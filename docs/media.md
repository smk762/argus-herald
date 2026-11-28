# Media and the clip registry

Any draft can carry images, GIFs or short clips, and each one needs alt text. On a draft's review page, upload a file or pick one from the media folder (`.argus-herald/media/` by default, or `media: {dir: …}`), and optionally attach it to every draft of the same post. From the shell:

```bash
argus-herald media add 12 ~/clips/atlas-coast.gif --alt "A coastline painted over real terrain, labels fading in" --all-drafts
argus-herald media list 12
argus-herald media alt 7 "Better description"
argus-herald compose "Coastlines, painted." --kind demo --media shot.png --alt "…"
```

**Metadata never leaves the machine.** Each file gets a cleaned copy, and only that copy is posted or handed off:

- **JPEG, PNG and WebP:** the EXIF rotation is applied, then EXIF (including GPS), XMP, IPTC, comments and text chunks are dropped. The colour profile is kept. A PNG that already holds nothing but its image data is copied byte for byte.
- **GIF:** frames and timing are kept, and comments are dropped.
- **MP4, MOV and WebM:** `ffmpeg -map_metadata -1 -c copy`. Without ffmpeg, a clip is attached but marked unstripped, and approve refuses it.

The review page says what was removed (for example "EXIF with GPS location, comment").

Approve is refused while any attachment lacks alt text, when the privacy gate flags alt text, or when the attachments don't fit the platform:

- **X:** 4 images, or 1 GIF or video; images up to 5 MB, GIFs up to 15 MB.
- **Patreon:** more generous limits.
- Platforms that can't attach media yet refuse drafts that carry it.

For X and Patreon, the handoff lists the cleaned files to download and attach, each with its alt text ready to copy.

## Clip registry

The registry records what each demo clip shows and what it was recorded against, so herald can say which demos have no current clip and which posts used which clip.

```bash
argus-herald clips add atlas-styles --demo atlas --project argus-atlas --title "Atlas styles" --format clip --target-seconds 30
argus-herald clips set atlas-styles --status recorded --path ~/clips/atlas-styles.mp4 \
    --project-version 0.9.0 --demo-repo argus-realms --demo-commit 16e992da7b
argus-herald clips                    # every clip, then the demos with no current clip
argus-herald clips show atlas-styles  # its fields and the posts that used it
argus-herald clips export clips.csv   # and `clips import clips.csv` to bring a spreadsheet back
```

- **Formats:** `loop`, `clip`, `short`, `walkthrough`. **Statuses:** `todo`, `scripted`, `recorded`, `edited`, `posted`, `stale`, `hold`. Herald never changes a status by itself.
- A demo counts as covered while one of its clips is `recorded`, `edited` or `posted`. A demo is known once any clip names it.
- Setting `--status recorded` fills in today's date unless `--recorded-at` is given.
- `clips import` checks every row before saving any. A column in the file replaces the stored value, and a column that isn't in the file is left alone. Columns the registry doesn't keep are named and skipped.
- `clips remove` refuses a clip that a post has used.

Attach a clip by its id and the link is recorded. The file is cleaned like any other attachment, and it still needs alt text:

```bash
argus-herald media add 12 --clip atlas-styles --alt "Three map styles applied to the same coastline" --all-drafts
argus-herald log-broadcast --platform reddit --url https://www.reddit.com/r/gis/comments/abc/ --clip atlas-styles
```

### Freshness

A clip is out of date when the live demo no longer looks like it. For each clip that is `recorded`, `edited` or `posted`, herald compares its `demo_commit` with the head of the demo repo's deploy branch (GitHub's compare API), and its `project_version` with the newest version herald has seen for `project`:

| State | When |
|---|---|
| `fresh` | no commits since `demo_commit`, same minor version |
| `check` | the demo repo changed, but not under its UI paths; or `demo_commit` isn't on the deploy branch; or the minor version moved |
| `redo` | files under the demo's UI paths changed |
| `unknown` | it couldn't be compared, for example no `demo_repo` or no owner for a bare repo name |

```bash
argus-herald clips refresh            # compare now (collect does this too)
argus-herald clips                    # redo first, then check, unknown, fresh; why, below the table
argus-herald clips ack atlas-styles   # it still matches: demo_commit and project_version move forward
argus-herald clips set atlas-styles --status stale   # it needs re-recording
```

- Herald never changes a clip's status. A `stale` clip isn't checked.
- `demo_repo` is `owner/repo`, or a bare repo name under `clips.owner` (then `github.owner`, then `pypi.discover.github_owner`).
- The deploy branch (default `dev`), the UI paths and the ignore paths are set under `clips:` in the config, with per-demo overrides under `clips.demos.<demo>`; a demo's own `branch`, `ui_paths` or `ignore_paths` replaces the global one rather than adding to it. A UI file matches a UI path and no ignore path. `**` crosses directories; a pattern without a `/` matches a file name at any depth, and one with a `/` is anchored at the repo root.
  - UI paths, by default front-end file types: `*.css`, `*.scss`, `*.html`, `*.tsx`, `*.jsx`, `*.ts`, `*.js`, `*.mjs`, `*.svelte`, `*.astro`, `*.vue`. A backend, docs or data-only commit (say `src/pkg/api.py`) asks for a check, not a re-record.
  - Ignore paths, never UI even when a UI path matches; by default `**/tests/**`, `**/test/**`, `**/__tests__/**`, `**/docs/**`, `**/scripts/**`, `**/e2e/**` (those directories at any depth), `*.test.*`, `*.spec.*`, `*.config.*` and `*.d.ts`.
  - Setting `ui_paths` keeps exactly your patterns, but the default ignore paths still apply to them unless you set `ignore_paths` too; `ignore_paths: []` turns ignoring off.
  - Stored results are recomputed on the next `clips refresh` or `collect`, so a change to these settings shows after that.
- No token is needed for public repos (GitHub allows 60 unauthenticated calls an hour); `GITHUB_TOKEN` is used when set. GitHub lists at most 300 changed files, and the listing says when it stopped there.
- A failed call keeps the last known state, and the listing says how old it is.
- `clips ack` needs a comparison first. Changing a clip's `demo_commit` or `project_version` by hand sets it back to not checked.

### Performance

Which clips did well, so the format can be repeated. A clip's posts are the drafts it is attached to that went out (published, or handed off and confirmed) and the hand-logged broadcasts that name it. Each post counts with its latest engagement sample (see [Engagement per post](metrics.md#engagement-per-post); X numbers come from the analytics import).

Per clip and platform, `clips` shows the posts and the sums of impressions, engagements and link clicks, and rates the clip against the account's own posts, so platforms with different audiences read the same way. The ratio is the median of the clip's posts over the median of every post on that platform from the last 30 days (the clip's own included). It compares impressions where the platform reports them, otherwise engagements (likes, reposts, replies and quotes where there is no engagements count).

| Rating | Ratio |
|---|---|
| `strong` | 1.5 or more |
| `typical` | between |
| `weak` | 0.5 or less |
| `no data` | no sample yet, only posts under 24 hours old, fewer than 5 baseline posts, or a baseline median of 0 |

```bash
argus-herald clips                    # a performance column (x strong, bsky typical), then the numbers per posted clip;
                                      # within each freshness state the best-performing clips come first
argus-herald clips show atlas-styles  # the same numbers for one clip
```

- A clip posted more than once (a re-cut, a repost) sums its posts and gets one rating per platform.
- A clip with no posts, or none sampled yet, is `no data`, never `weak`.
- A post younger than 24 hours is left out of both medians, since its first samples are small: it still counts in the clip's posts and sums, but a clip posted only that recently is `no data` (posted less than 24h ago). `min_age_hours` sets the age; 0 turns it off.
- It is worked out when you look, from `metrics.sqlite`; nothing is stored. Before the first `collect` everything is `no data`.
- The thresholds, the minimum baseline posts, the window and the minimum post age are under `clips.performance` in the config.
