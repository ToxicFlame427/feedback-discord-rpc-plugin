# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow [SemVer](https://semver.org/).

## [1.0.0] - 2026-11-10

First release.

### Added
- Discord Rich Presence while playing: song and artist, instrument (e.g. "Bass Guitar"), live score
  and accuracy (from the Note Detection plugin), tuning, and a song timer.
- Presence outside songs: "Paused • Bass Guitar" in the player, and labels such as "Browsing the song
  library" or "Tweaking settings" in the menus. Can be turned off.
- Album cover as the large image for songs Fee[dB]ack has matched to MusicBrainz, with the
  Fee[dB]ack logo as the fallback.
- Two timer styles: count-up ("Playing Fee[dB]ack", the default) or a progress bar ("Listening to
  Fee[dB]ack").
- Built-in official Discord Application ID, so users don't need a Discord developer account. A custom ID can
  be set under Advanced.
- Plugin panel with connection status, live preview and settings; opt-in (off by default).
- Dependency-free Discord IPC client (Unix socket / Windows named pipe) on a single worker thread,
  with rate limiting, de-duplication, auto-reconnect, stale-presence clearing, and graceful fallback
  for Discord clients that reject newer activity fields.
- Test suite with a fake Discord IPC server.
