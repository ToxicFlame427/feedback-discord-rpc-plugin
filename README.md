# Discord Rich Presence for Fee[dB]ack

Show what you're playing in [Fee[dB]ack](https://github.com/got-feedback/feedBack) on your Discord profile:
the song, your instrument, your live score, and the album cover. Friends see it in your profile and the
member list, and it updates as you play, pause, and browse.

```
[album cover]  Playing Fee[dB]ack
 [bass icon]   Hidden in the Sand by Tally Hall
               Bass Guitar • 12,345 pts (98.4%)
               0:15 elapsed
```

- **No setup on Discord's side.** It ships with the official Fee[dB]ack Discord app; you flip one switch.
- **No extra software.** It runs inside Fee[dB]ack, with no Python packages to install and no helper app to launch.
- **No changes to Fee[dB]ack.** It's a normal drop-in plugin.
- **Opt-in and private by default.** Nothing reaches Discord until you turn it on.

License: **AGPL-3.0-only**, same as Fee[dB]ack.

---

## Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Turning it on](#turning-it-on)
4. [What Discord shows](#what-discord-shows)
5. [Settings](#settings)
6. [Album covers and network use](#album-covers-and-network-use)
7. [Live score (Note Detection)](#live-score-note-detection)
8. [Troubleshooting](#troubleshooting)
9. [The Discord application (maintainers)](#the-discord-application-maintainers)
10. [Using your own Discord app](#using-your-own-discord-app)
11. [Repository layout](#repository-layout)
12. [How it works](#how-it-works)
13. [What it uses from Fee[dB]ack](#what-it-uses-from-feedback)
14. [Development and testing](#development-and-testing)
15. [Contributing](#contributing)

---

## Requirements

| | |
|---|---|
| **Fee[dB]ack** | v0.3 or newer (developed against `0.3.0-alpha.1`, the v3 UI) |
| **Discord** | The **desktop app** (Windows, macOS or Linux: `.deb`, Flatpak or Snap; Vesktop also works). Discord in a web browser can't receive Rich Presence. |
| **Where Fee[dB]ack runs** | On the **same computer** as Discord: `python main.py` or the Fee[dB]ack desktop app. See [Docker / NAS](#docker--nas). |
| **Discord setting** | *User Settings → Activity Privacy → Share your detected activities* turned **on** |
| **Python packages** | None beyond what Fee[dB]ack already installs |
| **Optional** | [Note Detection](https://github.com/got-feedback/feedBack-plugin-notedetect) plugin for the live score |

## Installation

Plugins live in Fee[dB]ack's `plugins/` folder, one folder per plugin.

**With git (recommended, since Fee[dB]ack can then update it for you):**

```bash
cd /path/to/feedBack
git clone https://github.com/<you>/feedBack-plugin-discord-rpc plugins/discord_rpc
```

**Without git:** download this repository as a ZIP (GitHub → **Code → Download ZIP**), extract it, and
rename the extracted folder to `discord_rpc` inside `plugins/`:

```
feedBack/
└── plugins/
    └── discord_rpc/        ← this repository
        ├── plugin.json
        ├── routes.py
        └── …
```

> The folder name doesn't strictly matter (Fee[dB]ack reads the `id` from `plugin.json`), but
> `discord_rpc` keeps things tidy.

Then **restart Fee[dB]ack**, because plugins are loaded at startup, and reload the page in your browser
(Ctrl+Shift+R).

**Updating:** `git pull` inside `plugins/discord_rpc`, or use Fee[dB]ack's plugin update button if
you installed with git. Restart Fee[dB]ack afterwards.

**Uninstalling:** delete the `plugins/discord_rpc` folder and restart. Your settings file
(`<config_dir>/discord_rpc.json`) can be deleted too.

## Turning it on

1. Make sure the Discord desktop app is open.
2. In Fee[dB]ack, open **Plugins → Discord** in the sidebar.
3. Flip **Broadcast Rich Presence** on.

The status in the top-right of the panel should read **Connected to Discord as *your name***. Start a
song and your profile updates within a few seconds. The **Live preview** on the panel shows exactly what's
being sent.

## What Discord shows

| Where you are | Discord card |
|---|---|
| **Playing a song** | album cover · `Song by Artist` · `Bass Guitar • 12,345 pts (98.4%)` · timer |
| **Paused in the player** | `Song by Artist` · `Paused • Bass Guitar` (no timer) |
| **Song library** | `Browsing the song library` · time since you opened Fee[dB]ack |
| **Other screens** | `On the home screen`, `Browsing playlists`, `Tweaking settings`, `In Career mode`, `Playing minigames`, … |
| **Broadcast off / Fee[dB]ack closed** | nothing (clears within 30 seconds at most) |

**Short lines, full details on hover.** Discord cuts long lines off with "…" at roughly 32 characters in
most places, so each line is kept to about that width. The song, instrument and score always come first.
Extras (tuning, song length) appear on a line only when they fit, and are **always** in the hover text of
the small instrument icon, e.g. `Bass Guitar • E Standard • 1:58`. Hovering the album cover shows the
album and year.

**Instruments** are named from the arrangement: Lead Guitar, Rhythm Guitar, Combo Guitar, Bass Guitar,
Drums, Keys, Vocals. Each has its own small icon.

**Timer styles:**

| Style | Header | Timer |
|---|---|---|
| **Count up** (default) | Playing Fee[dB]ack | `0:15 elapsed`; the song length goes beside the title when it fits |
| **Progress bar** | Listening to Fee[dB]ack | `0:15 ━━━━──── 1:58` |

Discord only draws a progress bar for "Listening"-type activities, so you get either the "Playing" header
or the bar, not both. At slowed practice speeds the bar's total stretches to real time, so it still
finishes exactly when the song does.

## Settings

All on **Plugins → Discord**. They're stored in `<config_dir>/discord_rpc.json` (by default
`~/.local/share/feedback/discord_rpc.json`) and included in Fee[dB]ack's **Settings → Export**.

| Setting | Default | What it does |
|---|---|---|
| Broadcast Rich Presence | **off** | Master switch. Nothing is sent to Discord while off |
| Show score & accuracy | on | Needs Note Detection (or another scorer) |
| Show tuning | on | e.g. "E Standard", "Drop D", using Fee[dB]ack's own tuning names |
| Show presence in menus & while paused | on | Off = only while a song is playing |
| Show album cover | on | Off = always the Fee[dB]ack logo |
| Show song timer | on | |
| Timer style | Count up | Count up ("Playing") or Progress bar ("Listening to") |
| Custom Application ID | *(empty)* | Advanced. Empty = the official Fee[dB]ack Discord app |

## Album covers and network use

When Fee[dB]ack's metadata matching has identified a song on **MusicBrainz**, the large image is that
release's front cover from the public **Cover Art Archive**. Otherwise the Fee[dB]ack logo appears.

- **Discord loads the image** from the public `coverartarchive.org` URL. Nothing on your computer is
  exposed, and Fee[dB]ack doesn't serve anything to Discord.
- If Fee[dB]ack already downloaded that cover (it does this by default for matched songs), the plugin
  uses it with **no extra network request**.
- For a matched song whose cover Fee[dB]ack didn't download, the plugin sends **one** `HEAD` request
  to the Cover Art Archive to check that the cover exists (only an album ID is sent, never anything about you). The result
  is cached, and the logo shows until the check passes.
- Setting `FEEDBACK_ENRICH_OFFLINE=1` (Fee[dB]ack's offline switch) stops that check too.
- The connection to Discord itself is a **local** socket / named pipe, not an internet request.

For comparison, Fee[dB]ack itself (by default) already contacts MusicBrainz and the Cover Art Archive for
metadata matching, runs `git fetch` for git-installed plugins, and loads its UI font from Google Fonts.

## Live score (Note Detection)

Fee[dB]ack core doesn't publish a score; scoring comes from the
[Note Detection](https://github.com/got-feedback/feedBack-plugin-notedetect) plugin. Install it into
`plugins/note_detect/` and turn detection on in the player; this plugin then reads
`window.noteDetect.getStats()` automatically every 4 seconds. Nothing needs configuring. Without a scorer,
the second line shows only the instrument (plus tuning if it fits).

**Other scoring plugins** can feed their running totals in either of two ways:

```js
// accuracy is a percentage (0–100)
window.feedBack.emit('discord_rpc:score', { score: 12345, accuracy: 98.4 });

// or, once this plugin has loaded (0–1 fraction also accepted)
window.feedBack.discordRpc && window.feedBack.discordRpc.reportScore({ score: 12345, accuracyFraction: 0.984 });
```

The score resets when a new song or arrangement loads.

## Troubleshooting

### Check the status on the panel first

| Status | Meaning / fix |
|---|---|
| **Connected to Discord as *you*** | Working. Changes reach Discord within a few seconds |
| Rich Presence is off | Flip **Broadcast Rich Presence** on |
| Discord Disconnected | Fee[dB]ack can't find the Discord app. Open the desktop app, or fully quit it (tray → Quit) and reopen. See [Docker / NAS](#docker--nas) |
| Discord rejected the connection | A **custom** Application ID is wrong. Clear it under *Advanced* and Save |
| Needs a Discord Application ID | Only possible if the built-in ID was removed from the code |

### Connected, but nothing on my profile

- **Activity Privacy:** *Discord → User Settings → Activity Privacy → Share your detected activities*
  must be on.
- **Looking in the right place:** click your own name or avatar in the desktop app. Discord sometimes
  doesn't show your own activity in the member list.
- **Missing images:** if the logo or instrument icons are blank, the art assets haven't been uploaded to
  the Discord app yet, or Discord is still caching them (allow 5–15 minutes). See
  [the Discord application](#the-discord-application-maintainers).

### Ask the plugin what it sees

With Fee[dB]ack running:

```bash
curl -s localhost:8000/api/plugins/discord_rpc/status
```

```json
{"ok": true, "state": "connected", "discord_user": "you",
 "endpoint": "/run/user/1000/discord-ipc-0", "enabled": true,
 "last_activity": {"details": "Hidden in the Sand by Tally Hall", "state": "Bass Guitar", ...},
 "last_update_at": 1794370391.1, "using_default_app": true}
```

- `endpoint` is the Discord socket it found. If it's `null`, Discord wasn't found.
- `last_activity` is exactly what Discord was last sent (`null` = cleared).

To check from the browser side, open the developer console (F12) on the Fee[dB]ack tab and run
`window.feedBack.discordRpc.getState()`. It shows whether the plugin thinks a song is playing, and what it
built.

### Docker / NAS

The plugin talks to Discord through a local socket, so the Fee[dB]ack **server** must be on the same
machine and user session as Discord.

| Setup | Works? |
|---|---|
| `python main.py` / desktop app on your PC | ✅ |
| Docker on the same Linux PC | ✅ if you mount the socket, e.g. in `docker-compose.yml`: `- ${XDG_RUNTIME_DIR}/discord-ipc-0:/tmp/discord-ipc-0` |
| NAS / Proxmox / another machine | ❌ the socket only exists on your PC |

Socket locations searched on Linux/macOS: `$XDG_RUNTIME_DIR`, `$TMPDIR`, `$TMP`, `$TEMP`, `/tmp`, plus the
Flatpak (`app/com.discordapp.Discord`), Snap (`snap.discord`) and Vesktop sub-folders, `discord-ipc-0`…`9`.
On Windows: `\\.\pipe\discord-ipc-0`…`9`.

## The Discord application (maintainers)

Rich Presence always belongs to a Discord "application": its **name** becomes the header ("Playing
**Fee[dB]ack**"), and it holds the uploaded images. The plugin ships with the official one:

```python
# presence.py
DEFAULT_CLIENT_ID = "1569927155264127106"
```

- An Application ID is a **public identifier**. Every Rich Presence game ships one, and it grants no
  control over the app. The plugin uses no client secret or bot token.
- The app should be owned by a **Discord Developer Team** rather than a personal account, so the
  project doesn't depend on one person. In the [Developer Portal](https://discord.com/developers/applications):
  **Teams → New Team**, invite maintainers, then open the app → **General Information → Transfer App to
  Team** (one-way). The ID doesn't change, so the plugin needs no update.

### Required art assets

Uploaded under the app's **Rich Presence → Art Assets**. Names must match exactly:

| Asset key | Used for | Suggested source |
|---|---|---|
| `app_logo` | large image when there's no album cover | Fee[dB]ack's `static/v3/brand/icon-512.png` (512×512) |
| `guitar_icon` | Lead / Rhythm / Combo / Guitar | instrument artwork, ≥ 512×512 |
| `bass_icon` | Bass Guitar | 〃 |
| `drums_icon` | Drums | 〃 |
| `keys_icon` | Keys / Piano | 〃 |
| `vocals_icon` | Vocals | 〃 |

New or changed assets can take a few minutes to show up because Discord caches them. Album covers need no
upload; they're loaded by URL.

## Using your own Discord app

For forks or custom branding:

1. [Developer Portal](https://discord.com/developers/applications) → **New Application**. Its name is what
   appears after "Playing".
2. Upload the [art assets](#required-art-assets) above.
3. Copy the **Application ID** from **General Information**.
4. In Fee[dB]ack: **Plugins → Discord → Advanced: use your own Discord app**, paste it, and click **Save**.

Clearing the field and saving switches back to the official app.

## Repository layout

```
feedBack-plugin-discord-rpc/
├── plugin.json              Manifest: id, nav entry, screen, script, styles, routes, settings export
├── routes.py                Backend entry point: setup(app, context). FastAPI routes, request
│                            validation, album-cover lookup; every handler is an error boundary
├── presence.py              The core logic (standard library only):
│                            • build_activity() — turns song state into Discord's activity fields
│                            • ConfigStore — discord_rpc.json, atomic writes, strict validation
│                            • CoverResolver — album art from Fee[dB]ack's MusicBrainz match
│                            • PresenceService — the single worker thread that talks to Discord
│                            • DEFAULT_CLIENT_ID — the official Discord application
├── discord_ipc.py           Minimal Discord IPC client (Unix socket / Windows named pipe), stdlib only
├── screen.js                Frontend: listens to Fee[dB]ack's event bus, builds the state,
│                            sends it to the backend, runs the Discord panel
├── screen.html              The Plugins → Discord panel: status, live preview, settings
├── settings.html            Small entry in Fee[dB]ack's Settings page linking to the panel
├── assets/
│   ├── discord-rpc.css      Panel styles (scoped under .drpc, uses Fee[dB]ack theme tokens)
│   └── thumb.svg            Plugin icon on Fee[dB]ack's Plugins page
├── tests/
│   └── test_discord_rpc.py  Test suite, including a fake Discord IPC server
├── .github/workflows/
│   └── tests.yml            CI: pytest (Linux + Windows), JS syntax check, manifest check
├── CHANGELOG.md
├── LICENSE                  AGPL-3.0
└── README.md
```

**Common edits:**

| To change… | Edit |
|---|---|
| Menu labels ("Browsing the song library", …) | `SCREEN_LABELS` at the top of `screen.js` |
| Instrument names / icon keys | `INSTRUMENTS` in `screen.js` and `_INSTRUMENTS` in `presence.py` |
| Line layout, separators, width | `build_activity()` and `COMPACT_WIDTH` in `presence.py` |
| Update frequency | `UPDATE_INTERVAL_MS` / `IDLE_INTERVAL_MS` in `screen.js` |
| Default settings | `DEFAULT_CONFIG` in `presence.py` |
| Official Discord app | `DEFAULT_CLIENT_ID` in `presence.py` |

## How it works

```
 Fee[dB]ack (browser)                     Fee[dB]ack server (Python)                       Discord app
┌──────────────────────┐  POST /presence  ┌───────────────────────────────────────────┐  local IPC  ┌─────────┐
│ screen.js            │ ───────────────▶ │ routes.py → build_activity()              │             │         │
│  • song/screen events│  every 4 s       │            ↓                              │             │         │
│  • audio clock       │  (15 s in menus) │ PresenceService (1 worker thread)         │ ──────────▶ │ profile │
│  • Note Detection    │                  │  • keeps only the newest state            │ SET_ACTIVITY│         │
│                      │  POST /clear     │  • ≤ 5 updates / 20 s (Discord's limit)   │             │         │
│                      │ ───────────────▶ │  • skips updates that change nothing      │             │         │
└──────────────────────┘                  │  • reconnects with backoff (2 s → 60 s)   │             └─────────┘
                                          │  • clears if not refreshed for 30 s       │
                                          └───────────────────────────────────────────┘
```

**Frontend (`screen.js`).** Listens on Fee[dB]ack's event bus (`window.feedBack`):

| Event | Action |
|---|---|
| `song:loaded` | read title, artist, arrangement → instrument, tuning |
| `song:play` / `song:resume` | playing presence, refreshed every 4 s (live score and position) |
| `song:seek` | resend soon (debounced) so the timer jumps too |
| `song:pause` / `song:ended` / `song:stop` | switch to "Paused" (or clear, if menu presence is off) |
| `screen:changed` | switch to that screen's label, refreshed every 15 s |
| page closed | best-effort clear via `navigator.sendBeacon` |

The playback position is read from the live audio clock (`#audio`, or the desktop app's JUCE player) at send
time, not estimated.

**Backend (`routes.py`, `presence.py`).** HTTP handlers never touch Discord directly: they build the activity,
hand it to `PresenceService`, and return immediately. The single worker thread owns the connection, which
means Fee[dB]ack's request handling can never be slowed down or broken by Discord. If Discord is closed, quits
mid-song, restarts, or rejects something, the result is a status message, never an exception.

**Compatibility fallbacks.** If a Discord client rejects a newer field (the "Listening" activity type for the
progress bar, or an image URL for the album cover), the plugin drops that one feature, retries straight
away, and remembers it for the session. You get "Playing" / the logo instead of a blank status.

### HTTP endpoints

All under `/api/plugins/discord_rpc/`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/presence` | State payload → Discord activity. Returns the exact activity built |
| POST | `/clear` | Clear the presence |
| GET | `/status` | Connection state, Discord username, socket path, last activity sent |
| GET | `/config` | Current settings |
| POST | `/config` | Update settings (any subset) |

`/presence` payload (all optional): `mode` (`playing` / `paused` / `browsing`), `activityLabel` and
`sessionStart` (browsing), `filename` (library key, for the cover lookup), `songTitle`, `artist`,
`instrument`, `instrumentKey`, `tuning`, `score`, `accuracy` (percent), `duration`, `currentTime`
(seconds), `playbackRate`.

`/config` keys: `enabled`, `client_id`, `show_score`, `show_tuning`, `show_idle`, `show_cover`,
`show_timestamps`, `timer_style` (`elapsed` / `bar`). Only real JSON booleans toggle settings (a string
`"false"` is ignored), and an invalid `client_id` returns HTTP 400.

> **Why `/presence` and not `/update`?** Fee[dB]ack core owns `POST /api/plugins/<id>/update` (its plugin
> self-updater, which runs `git pull`) and registers it before plugins load, so a plugin route with that
> name is unreachable. A test (`test_no_route_collides_with_core_plugin_routes`) guards against this.

## What it uses from Fee[dB]ack

**No Fee[dB]ack code is modified.** The plugin relies only on Fee[dB]ack's documented plugin contract
(see Fee[dB]ack's `CLAUDE.md` → *Plugin System*):

| Side | Uses |
|---|---|
| Manifest | `nav`, `screen`, `script`, `styles`, `routes`, `settings.html`, `settings.server_files`, `icon` |
| Backend `context` | `config_dir` (settings file), `log` (logging), `load_sibling` (module loading), `meta_db.get_enrichment()` (album cover lookup, read-only) |
| Frontend | `window.feedBack` event bus (`song:*`, `screen:changed`), `window.feedBack.currentSong`, `window.feedBack.displayTuningName()`, the `#audio` element / `window.jucePlayer`, `window.showScreen()` |
| Optional | `window.noteDetect.getStats()` from the Note Detection plugin |

Everything the plugin writes lives in one file: `<config_dir>/discord_rpc.json`. Every lookup of a Fee[dB]ack
feature is guarded, so a missing feature degrades gracefully (no tuning name, no cover, no score) instead of
erroring.

## Development and testing

```bash
# from the repository root (or plugins/discord_rpc inside Fee[dB]ack)
python -m pip install pytest fastapi httpx   # fastapi/httpx only for the HTTP-route test
python -m pytest tests -q
node --check screen.js
```

- A **fake Discord** listens on a Unix socket in a temporary `$XDG_RUNTIME_DIR`, so the real IPC client is
  tested end to end, covering the handshake, set/clear, an invalid ID, Discord quitting and restarting, and rejected
  fields. Socket tests skip on Windows; everything else runs there too.
- No test touches the network: the cover check is injected, and `FEEDBACK_ENRICH_OFFLINE` is honoured.
- CI (`.github/workflows/tests.yml`) runs all of this on Linux and Windows for every push and pull request.

**Trying changes live:** edit the files in `plugins/discord_rpc/`, restart Fee[dB]ack for Python changes,
and hard-reload the browser (Ctrl+Shift+R) for `screen.js` / `screen.html` / CSS changes. The panel's
**Live preview** and `GET /status` show the result without needing to check Discord.

## Contributing

Issues and pull requests are welcome.

- Fee[dB]ack requires a **DCO sign-off** on commits: `git commit -s`.
- Add a line to `CHANGELOG.md` under an `[Unreleased]` heading.
- Keep it dependency-free (standard library on the Python side, vanilla JS in the browser), and keep the
  error boundaries: Discord problems must never surface as Fee[dB]ack errors.
- Run the tests before opening a PR.

By contributing you agree that your contribution is licensed under AGPL-3.0-only.
