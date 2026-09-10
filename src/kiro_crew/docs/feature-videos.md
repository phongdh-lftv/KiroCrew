# Feature Videos

Kiro Crew plays a short intro clip for a feature this install has not used yet. Unlike [Feature Tips](feature-tips.md), which are written by a model, a feature video is a recorded clip picked by a fixed rule set: the same install state always produces the same set of *eligible* clips, and one of them is chosen at random.

Clips are hosted, not shipped inside the package. Kiro Crew downloads them in the background and plays them from your own disk — see [Hosted Clips](#hosted-clips).

## How It Works

- The catalog is data, not generated: a signed manifest published for your release, with a small built-in list as the fallback for an install that has never fetched one. There is no model call.
- A clip is eligible when the feature is on, its media is on your machine (or can be streamed from the manifest's CDN), it is not yet recorded as seen or dismissed, the running version satisfies its floor, and no "you already use this" signal fires.
- Which eligible clip you get is random. Eligibility is not: a clip you retired, or one for a feature you already use, can never come back.
- A clip already on your disk always wins over one that would have to stream, so playback starts immediately and costs no bandwidth.
- An entry whose media is not shipped is withheld, not shown. The dialog opens on the JSON answer alone and fetches nothing until the user presses play, so it cannot detect a missing clip itself -- it would open around a blank player, and the verdict a user then records is permanent. Withholding keeps the entry on offer for the launch after its clip lands.
- One function checks every clip source. A downloaded clip is served from your own machine under `/feature-videos/<release>/`; a built-in one from `/app-assets/feature-videos/`. The only off-machine source allowed is the exact CDN host the signed manifest names, and only for a clip that is not downloaded yet — no other host, no credentials in the URL, no query string.
- Seen and dismissed are both permanent. There is no snooze: a feature intro that comes back is noise.
- Temporary and incognito sessions get no video, because the state a video records is permanent and instance-wide.
- A clip whose `min_version` is above the running version is skipped, so a video recorded ahead of a release never plays on an older build.

## Controls

| Action | Effect |
|--------|--------|
| Watch a clip to the end | Records `seen`; that video is never offered again. |
| Close the modal | Records `dismissed`; same permanence. |
| `dashboard.feature_videos_enabled: true` in config | Turns the feature ON. It is OFF by default until real clips ship. |
| "Download all" in Settings | Fetches every clip for your release now, without the background rate limit. |

## Hosted Clips

Clips live on a CDN, listed by a **signed manifest** for your release. Kiro Crew
downloads them into `~/.kiro/crew/feature-videos/<release>/` (readable only by you)
and plays them from there.

### What arrives, and when

- At gateway start, a background task fetches the manifest, then downloads any clip
  you do not already have — one at a time, capped at 512 KiB/s so it stays out of
  your way. An interrupted download resumes on the next start.
- Every file is checked against the SHA-256 in the signed manifest before it is
  installed. A tampered or truncated file is discarded, never played.
- If a clip is eligible but not downloaded yet, it streams from the CDN host the
  manifest names. Once it is on disk, the local copy is used.
- "Download all" in Settings does the same pass immediately, without the rate limit.

### Trust

- The manifest is signed with the same offline key that signs Kiro Crew's own
  installer. If the **signature** does not verify, the whole manifest is discarded
  and the built-in list is used: the signature covers the document, so there is no
  such thing as verifying most of it. A malformed **entry** inside a correctly
  signed manifest is a different case — that entry alone is dropped, the reason is
  logged, and the rest of the list plays.
- The cached manifest on your disk is re-verified every time it is read, so editing it
  cannot change where clips are fetched from.
- A clip and its poster are checked against the manifest's sha256 when they are
  downloaded. After that the check is presence and byte size, not a re-hash: playing a
  clip does not read it twice. So a clip file edited on disk afterwards, by something
  that already has write access to your own home directory, is played as it stands. What
  it cannot do is change where anything is fetched from, or point Kiro Crew at a file
  outside the cache.
- The route that serves a cached file answers only for a plain file directly inside the
  release folder, under the canonical cache directory. A symbolic link is refused rather
  than followed, at the file and at the release folder both, because a manifest names
  files and a link is not a file a manifest can describe.
- If there is no manifest for your release, Kiro Crew looks for the newest lower one:
  the same minor line first, then up to three earlier minors. It never looks across a
  major version, since a clip from a different major is likely to show a UI that no
  longer exists.

### Disk use

`dashboard.feature_videos_cache_max_mb` (default 500) is the budget. When the cache
is over it, whole release folders are removed oldest first. The release you are
running is never removed, even if it alone exceeds the budget — deleting the clips
you are about to play would just download them again.
`dashboard.feature_videos_keep_releases` (default 0 = no limit) additionally caps how
many release folders are kept.

### Turning the download off

Two independent switches:

- `dashboard.feature_videos_enabled: false` (the default) turns the whole feature
  off: nothing is fetched and nothing is shown.
- A managed fleet can forbid the fetch itself with the
  `capabilities.feature_videos_download` policy scope. Then no manifest is requested,
  no clip is downloaded, and nothing streams from the CDN — clips already on disk
  still play. The dashboard reads the answer from `GET /api/dashboard/config`
  (`feature_videos_download_enabled`), so the controls reflect it rather than failing
  when pressed.

For a mirrored or air-gapped install, point `dashboard.feature_videos_manifest_url`
(or `KIROCREW_FEATURE_VIDEOS_MANIFEST_URL`) at your own copy. It must be `https`, and
the signature is still checked — a mirror can relocate the files, not introduce new
ones.

## Adding a Catalog Entry

This is the **fallback** list, used by an install that has never fetched a manifest; a new clip normally goes into the published manifest instead, which needs no release. Append a `VideoEntry` to `CATALOG` in `src/kiro_crew/feature_videos.py`. Order no longer decides what is shown — the pick among eligible entries is random.

```python
VideoEntry(
    id="knowledge-library",
    feature="knowledge-library",
    title="Search your own documents",
    description="One or two plain sentences on what the feature does.",
    src="/app-assets/feature-videos/knowledge-library.mp4",
    poster="/app-assets/feature-videos/knowledge-library.jpg",
    duration_s=20.0,
    doc="knowledge-library-how-it-works.md",
    used_when=("config_key_set:knowledge.enabled",),
    min_version="",
)
```

Rules the catalog enforces, each of which drops the entry with a logged warning rather than breaking the endpoint:

- `id` is a slug and doubles as the state key and the asset basename; keep `src` and `poster` as `<id>.mp4` and `<id>.jpg`.
- `doc` must be listed in `tips_allowlist.py`, the same allowlist tips use — a video cannot point at an internal design note.
- `src` and `poster` must pass `validate_asset_path`.
- `min_version` is optional and must parse as a version when present.

Place the clip and its poster in `website/public/app-assets/feature-videos/`.

## Declaring a `used_when` Signal

`used_when` names deterministic probes. Any one of them firing withdraws the video, because an intro for a feature already in use is worse than no intro. A probe that raises, or a signal nobody registered, counts as "not used" — the clip still plays, and the reason is logged.

Shipped signals:

| Signal | Fires when |
|--------|-----------|
| `tips_feedback_exists` | The user has reacted to a feature tip in any way. |
| `artifacts_nonempty` | The artifact library holds at least one artifact. |
| `sel_event_seen:<tool_name>` | A recent audit-log row names that tool, e.g. `sel_event_seen:monitor_start`. |
| `config_key_set:<dotted.path>` | The user set that key in `config.json` or `config.local.json`. Presence in the file, not the effective value, so a shipped default never fires it. |

To add one, register a function in `_PROBES` (no argument) or `_PARAM_PROBES` (the part after the first `:` is passed in). Keep it cheap: probes run on a polled route, at most once per `/api/feature-videos/next` request, and only for entries no earlier check has already ruled out.

## Configuration

```yaml
dashboard:
  feature_videos_enabled: true      # instance-wide switch; DEFAULT false
  feature_videos_manifest_url: ""   # https override; empty = the public CDN
  feature_videos_cache_max_mb: 500   # disk budget; 0 = no cap
  feature_videos_keep_releases: 0    # release folders to keep; 0 = no limit
```

Display state lives in `feature_videos_state.json` beside `tips_state.json`, and downloaded clips in `feature-videos/<release>/` — both written with owner-only permissions.

See [Configuration Reference](configuration.md) for the full list.
