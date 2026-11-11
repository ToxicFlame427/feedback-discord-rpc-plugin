# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Fee[dB]ack contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by the
# Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details. You should have received a copy of the GNU Affero General Public
# License along with this program. If not, see <https://www.gnu.org/licenses/>.
"""Discord Rich Presence — activity mapping, config store and IPC worker.

Pure standard library (no FastAPI import) so it is unit-testable on its own.

Threading model
---------------
HTTP handlers never touch the IPC socket. They hand the *latest desired state*
to ``PresenceService`` and return immediately. One daemon worker thread owns
the connection and:

  * connects / reconnects with exponential backoff (2 s → 60 s),
  * coalesces bursts — only the newest desired state is ever sent,
  * enforces Discord's SET_ACTIVITY budget (5 updates per 20 s) with a token
    bucket, so a clear right after an update still goes out immediately,
  * skips sends whose only difference is <2 s of timestamp drift,
  * clears a presence nobody has refreshed for ``STALE_AFTER_S`` (tab closed
    mid-song, frontend crashed),
  * swallows every IPC failure into ``status()`` — Discord being closed is a
    normal state, never an exception that reaches Fee[dB]ack.
"""

import json
import logging
import os
import re
import threading
import time

# The inofficial Fee[dB]ack Discord application. Application IDs are public
# identifiers (every Rich Presence game ships one) — this grants no control
# over the app. Owned by a Discord Developer Team so it isn't tied to one
# person; maintainers manage its name and art assets in the Developer Portal.
DEFAULT_CLIENT_ID = "1569927155264127106"

DEFAULT_CONFIG = {
    "enabled": False,          # opt-in: nothing is broadcast until the user enables it
    "client_id": "",           # optional override; "" = use DEFAULT_CLIENT_ID
    "show_score": True,
    "show_tuning": True,
    "show_idle": True,
    # "elapsed": header stays "Playing Fee[dB]ack", timer counts up ("0:15 elapsed")
    #            and the song length is added to the title line.
    # "bar":     elapsed/total progress bar — Discord only draws it for the
    #            Listening type, so the header becomes "Listening to Fee[dB]ack".
    "timer_style": "elapsed",
    "show_cover": True,        # album art as the large image when Fee[dB]ack knows the release         # presence in menus ("Browsing the song library") and while paused
    "show_timestamps": True,
}

TIMER_STYLES = ("elapsed", "bar")
CLIENT_ID_RE = re.compile(r"^\d{15,25}$")
ASSET_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")

LARGE_IMAGE_KEY = "app_logo"
LARGE_IMAGE_TEXT = "Fee[dB]ack"
TEXT_MIN, TEXT_MAX = 2, 128     # Discord's limits for details/state/*_text
TIMESTAMP_TOLERANCE_MS = 2000

# Discord activity types. LISTENING + start/end timestamps renders a progress
# bar with elapsed and total time ("0:01 ━━━━──── 1:57") under the header
# "Listening to Fee[dB]ack". Without timestamps there is no bar, so PLAYING.
ACTIVITY_PLAYING = 0
ACTIVITY_LISTENING = 2
STALE_AFTER_S = 30.0

# (substring in lower-cased instrument name, canonical display name, asset key)
_INSTRUMENTS = (
    ("bass", "Bass Guitar", "bass_icon"),
    ("drum", "Drums", "drums_icon"),
    ("key", "Keys", "keys_icon"),
    ("piano", "Keys", "keys_icon"),
    ("vocal", "Vocals", "vocals_icon"),
    ("lead", "Lead Guitar", "guitar_icon"),
    ("rhythm", "Rhythm Guitar", "guitar_icon"),
    ("combo", "Combo Guitar", "guitar_icon"),
    ("guitar", "Guitar", "guitar_icon"),
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")


# ── pure helpers ────────────────────────────────────────────────────────────

def clean_text(value, max_len=TEXT_MAX):
    """Strip control chars, collapse whitespace, clamp to Discord's limits."""
    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub("", str(value or ""))).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def fit_text(value, max_len=TEXT_MAX):
    """``clean_text`` plus padding: Discord rejects fields shorter than 2 chars."""
    text = clean_text(value, max_len)
    while 0 < len(text) < TEXT_MIN:
        text += "​"  # zero-width space keeps short titles like "X" valid
    return text


def resolve_instrument(name, key=None):
    """Return ``(display_name, asset_key)`` for an instrument/arrangement name."""
    display = clean_text(name, 48)
    lowered = display.lower()
    derived_key = "guitar_icon"
    for needle, canonical, asset in _INSTRUMENTS:
        if needle in lowered:
            derived_key = asset
            if not display:
                display = canonical
            break
    if not display:
        display = "Guitar"
    if key and ASSET_KEY_RE.match(str(key)):
        derived_key = str(key)
    return display, derived_key


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number


COMPACT_WIDTH = 32   # ≈ what Discord's pop-out / member list shows before "…"
SEP = " \u2022 "


def format_accuracy(accuracy):
    accuracy = min(max(accuracy, 0.0), 100.0)
    text = "%.1f" % accuracy
    return (text[:-2] if text.endswith(".0") else text) + "%"   # "98%" not "98.0%"


def format_score(score=None, accuracy=None):
    """``"12,345 pts (98.4%)"``, or ``None`` when there's no score yet."""
    score, accuracy = _num(score), _num(accuracy)
    if score is None:
        return None
    text = "%s pts" % "{:,}".format(int(round(max(score, 0))))
    if accuracy is not None:
        text += " (%s)" % format_accuracy(accuracy)
    return text


def format_state(instrument, score=None, accuracy=None, show_score=True, tuning=None):
    """Full state line, every part: ``"{instrument} • {tuning} • {score}"``."""
    parts = [instrument, clean_text(tuning, 32)]
    if show_score:
        parts.append(format_score(score, accuracy))
    return SEP.join(p for p in parts if p)


def fit_parts(required, optional, width=COMPACT_WIDTH):
    """Join ``required`` parts, then add each ``optional`` part only while the
    line still fits ``width`` — so the important bit is never the one cut."""
    parts = [p for p in required if p]
    for extra in optional:
        if extra and len(SEP.join(parts + [extra])) <= width:
            parts.append(extra)
    return SEP.join(parts)


def format_length(seconds):
    seconds = int(round(max(seconds, 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def build_activity(payload, now=None, show_score=True, show_timestamps=True, show_tuning=True,
                   timer_style="elapsed", cover=None):
    """Map a frontend state payload to a Discord activity dict.

    ``payload`` keys (all optional): mode, songTitle, artist, instrument,
    instrumentKey, tuning, score, accuracy (percent, 0–100), duration,
    currentTime (seconds), playbackRate (1.0 = normal speed).
    ``cover``: optional ``{"url": ..., "text": ...}`` album art for the large image.

    The two visible lines are kept short (COMPACT_WIDTH) because Discord cuts
    most views off around there. Details that don't fit — tuning, song
    length — always remain in the instrument icon's hover text.
    """
    now = time.time() if now is None else now
    mode = payload.get("mode") or "playing"
    if mode == "browsing":
        return build_browsing_activity(payload, show_timestamps)
    title = clean_text(payload.get("songTitle")) or "Unknown song"
    artist = clean_text(payload.get("artist"))
    tuning = clean_text(payload.get("tuning"), 32) if show_tuning else ""
    instrument, icon_key = resolve_instrument(payload.get("instrument"), payload.get("instrumentKey"))
    duration = _num(payload.get("duration"))
    length = format_length(duration) if duration and duration > 0 else ""

    # Line 1: "Title by Artist", plus "(1:58)" when the count-up timer is used
    # and it still fits (the progress-bar style shows the total itself).
    song_line = "%s by %s" % (title, artist) if artist else title
    if (mode == "playing" and show_timestamps and timer_style != "bar" and length
            and len(song_line) + len(length) + 3 <= COMPACT_WIDTH):
        song_line += " (%s)" % length

    # Line 2: instrument + score come first; tuning only if it still fits.
    score_text = format_score(payload.get("score"), payload.get("accuracy")) if show_score else None
    lead = ["Paused", instrument] if mode == "paused" else [instrument]
    state_line = fit_parts(lead + [score_text], [tuning])

    activity = {
        "details": fit_text(song_line),
        "state": fit_text(state_line),
        "assets": {
            "large_image": LARGE_IMAGE_KEY,
            "large_text": LARGE_IMAGE_TEXT,
            "small_image": icon_key,
            # Hover text keeps everything, whatever the lines had to drop.
            "small_text": fit_text(SEP.join(p for p in (instrument, tuning, length) if p)),
        },
        "instance": False,
    }
    if cover and cover.get("url"):
        activity["assets"]["large_image"] = cover["url"]
        activity["assets"]["large_text"] = fit_text(cover.get("text") or LARGE_IMAGE_TEXT)

    current = _num(payload.get("currentTime"))
    rate = _num(payload.get("playbackRate")) or 1.0
    rate = min(max(rate, 0.1), 4.0)
    if mode == "playing" and show_timestamps and current is not None and current >= 0:
        # Spec: start = now - currentTime, end = now + (duration - currentTime).
        # Divided by the playback rate so the bar stays truthful during
        # slowed-down practice (identical at 1.0x).
        start = now - current / rate
        timestamps = {"start": int(start * 1000)}
        if timer_style == "bar" and duration and duration > current:
            timestamps["end"] = int((now + (duration - current) / rate) * 1000)
            activity["type"] = ACTIVITY_LISTENING   # needed for Discord's progress bar
        activity["timestamps"] = timestamps         # start only → counts up: "0:15 elapsed"
    return activity


def build_browsing_activity(payload, show_timestamps=True):
    """Presence while in the menus: ``"Browsing the song library"`` plus an
    elapsed timer counting from when Fee[dB]ack was opened."""
    activity = {
        "details": fit_text(payload.get("activityLabel") or "Browsing the menus"),
        "assets": {"large_image": LARGE_IMAGE_KEY, "large_text": LARGE_IMAGE_TEXT},
        "instance": False,
    }
    started = _num(payload.get("sessionStart"))
    if show_timestamps and started and started > 0:
        activity["timestamps"] = {"start": int(started * 1000)}
    return activity


def same_activity(a, b, tolerance_ms=TIMESTAMP_TOLERANCE_MS):
    """True when two activities differ only by sub-tolerance timestamp drift."""
    if a is None or b is None:
        return a is b
    strip = lambda act: {k: v for k, v in act.items() if k != "timestamps"}  # noqa: E731
    if strip(a) != strip(b):
        return False
    ta, tb = a.get("timestamps") or {}, b.get("timestamps") or {}
    if ta.keys() != tb.keys():
        return False
    return all(abs(ta[k] - tb[k]) <= tolerance_ms for k in ta)


# ── album covers ───────────────────────────────────────────────────────────

CAA_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COVER_URL = "https://coverartarchive.org/release/%s/front-250"


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def network_allowed():
    """Honour Fee[dB]ack's own offline switches (lib/enrichment.py)."""
    return not (_env_flag("FEEDBACK_ENRICH_OFFLINE") or _env_flag("FEEDBACK_SKIP_STARTUP_TASKS"))


def caa_has_front(release_id, timeout=6.0):
    """True/False when the Cover Art Archive answers, None on network trouble."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(COVER_URL % release_id, method="HEAD",
                                 headers={"User-Agent": "FeedBack-DiscordRPC/1.0 (+https://github.com/got-feedback/feedBack)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # follows the redirect to the image host
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (OSError, ValueError):
        return None


class CoverResolver:
    """Album art for the Discord large image, without ever blocking a request.

    Uses the MusicBrainz release Fee[dB]ack's enrichment already matched
    (``meta_db.get_enrichment(filename)``). Discord fetches the image itself
    from the public Cover Art Archive URL, so nothing local is exposed.

      art_state 'caa'          → Fee[dB]ack fetched that cover: use it.
      art_state 'none'/'error' → no cover exists: keep the logo.
      anything else            → check once in the background (HEAD), cache it;
                                 the logo shows until the check passes.
    """

    RETRY_AFTER_S = 600.0

    def __init__(self, meta_db, checker=caa_has_front, log=None, clock=time.monotonic):
        self._db = meta_db
        self._checker = checker
        self._log = log or logging.getLogger("feedBack.plugin.discord_rpc")
        self._clock = clock
        self._lock = threading.Lock()
        self._known = {}        # release_id -> (bool | None, checked_at)
        self._pending = set()

    def lookup(self, filename):
        if not filename or self._db is None or not hasattr(self._db, "get_enrichment"):
            return None
        try:
            row = self._db.get_enrichment(filename)
        except Exception:  # noqa: BLE001 — art is decoration; never fail a presence update
            return None
        if not row or row.get("match_state") not in ("matched", "manual"):
            return None
        release_id = str(row.get("mb_release_id") or "").lower()
        if not CAA_ID_RE.match(release_id):
            return None
        state = row.get("art_state")
        if state in ("none", "error"):
            return None
        if state != "caa" and not self._verified(release_id):
            return None
        album = clean_text(row.get("canon_album"), 100)
        year = clean_text(row.get("canon_year"), 4)
        text = "%s (%s)" % (album, year) if album and year else album
        return {"url": COVER_URL % release_id, "text": text or None}

    def _verified(self, release_id):
        with self._lock:
            known = self._known.get(release_id)
            if known is not None:
                ok, at = known
                if ok is not None or self._clock() - at < self.RETRY_AFTER_S:
                    return bool(ok)
            if release_id in self._pending or not network_allowed():
                return False
            self._pending.add(release_id)
        threading.Thread(target=self._check, args=(release_id,), daemon=True,
                         name="discord-rpc-cover").start()
        return False

    def _check(self, release_id):
        try:
            ok = self._checker(release_id)
        except Exception:  # noqa: BLE001
            ok = None
        with self._lock:
            self._known[release_id] = (ok, self._clock())
            self._pending.discard(release_id)
        self._log.debug("discord_rpc: cover for %s: %s", release_id, ok)


# ── config persistence ─────────────────────────────────────────────────────

def effective_client_id(cfg):
    """The user's own Application ID if they set one, else the built-in app."""
    return (cfg.get("client_id") or "").strip() or DEFAULT_CLIENT_ID


class ConfigStore:
    """``<config_dir>/discord_rpc.json`` with atomic writes and safe defaults."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()

    def read(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cfg
        if isinstance(data, dict):
            cfg.update(self.sanitize(data, partial=True))
        return cfg

    @staticmethod
    def sanitize(data, partial=False):
        """Keep only known keys with correctly-typed values (fail closed)."""
        out = {}
        for key in ("enabled", "show_score", "show_timestamps", "show_tuning", "show_idle", "show_cover"):
            if isinstance(data.get(key), bool):  # "false"/"0" never coerce to True
                out[key] = data[key]
        if data.get("timer_style") in TIMER_STYLES:
            out["timer_style"] = data["timer_style"]
        if "client_id" in data:
            cid = str(data.get("client_id") or "").strip()
            if cid == "" or CLIENT_ID_RE.match(cid):
                out["client_id"] = cid
            elif not partial:
                raise ValueError("client_id must be a numeric Discord Application ID")
        return out

    def update(self, changes):
        with self._lock:
            cfg = self.read()
            cfg.update(self.sanitize(changes))
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            os.replace(tmp, self.path)
            return cfg


# ── worker ─────────────────────────────────────────────────────────────────

_UNSET = object()


class _TokenBucket:
    def __init__(self, capacity, window_s, clock):
        self.capacity, self.window, self.clock = capacity, window_s, clock
        self._stamps = []

    def wait_time(self):
        now = self.clock()
        self._stamps = [t for t in self._stamps if now - t < self.window]
        if len(self._stamps) < self.capacity:
            return 0.0
        return self.window - (now - self._stamps[0])

    def take(self):
        self._stamps.append(self.clock())


class PresenceService:
    RATE_CAPACITY = 5
    RATE_WINDOW_S = 20.0
    BACKOFF_MIN_S = 2.0
    BACKOFF_MAX_S = 60.0
    IDLE_POLL_S = 15.0

    def __init__(self, store, ipc_factory, rejected_errors=(), log=None,
                 clock=time.monotonic, wall=time.time):
        self.store = store
        self._ipc_factory = ipc_factory      # client_id -> DiscordIPC-like object
        # Exceptions meaning "Discord answered and said no" (bad client id,
        # invalid payload). Retrying those quickly is pointless, so they are
        # logged once and backed off hard instead of treated as "Discord closed".
        self._rejected = tuple(rejected_errors)
        self.log = log or logging.getLogger("feedBack.plugin.discord_rpc")
        self._clock, self._wall = clock, wall
        self._cond = threading.Condition()
        self._thread = None
        self._stop = False

        # desired state (guarded by _cond)
        self._desired = None                 # activity dict, or None = cleared
        self._dirty = False
        self._config_dirty = True
        self._last_submit = 0.0

        # worker-owned
        self._ipc = None
        self._client_id = None
        self._applied = _UNSET
        self._next_retry = 0.0
        self._backoff = self.BACKOFF_MIN_S
        self._bucket = _TokenBucket(self.RATE_CAPACITY, self.RATE_WINDOW_S, clock)
        self._last_warning = None
        self._type_supported = True          # flips off if Discord rejects activity.type
        self._urls_supported = True          # flips off if Discord rejects image URLs

        self._status = {
            "state": "disabled", "message": "", "endpoint": None, "discord_user": None,
            "last_activity": None, "last_update_at": None,
        }

    # ── public API (thread-safe, non-blocking) ──────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="discord-rpc-ipc", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout)

    def submit(self, activity):
        """Queue an activity dict (or ``None`` to clear). Returns immediately."""
        with self._cond:
            self._desired = activity
            self._dirty = True
            self._last_submit = self._clock()
            self._cond.notify_all()

    def clear(self):
        self.submit(None)

    def reconfigure(self):
        with self._cond:
            self._config_dirty = True
            self._dirty = True
            self._cond.notify_all()

    def status(self):
        with self._cond:
            snap = dict(self._status)
        cfg = self.store.read()
        snap["enabled"] = cfg["enabled"]
        snap["client_id_set"] = bool(effective_client_id(cfg))
        snap["using_default_app"] = not cfg["client_id"]
        return snap

    # ── worker loop ────────────────────────────────────────────────────────
    def _set_status(self, state, message="", **extra):
        with self._cond:
            if state != self._status["state"]:
                self.log.info("discord_rpc: %s%s", state, (" — " + message) if message else "")
            self._status["state"] = state
            self._status["message"] = message
            self._status.update(extra)

    def _run(self):
        cfg = self.store.read()
        while True:
            with self._cond:
                if not self._stop:
                    self._cond.wait(self._wait_timeout(cfg))
                if self._stop:
                    break
                if self._config_dirty:
                    cfg = self.store.read()
                    self._config_dirty = False
                desired, dirty = self._desired, self._dirty
                stale = (desired is not None and self._clock() - self._last_submit > STALE_AFTER_S)
                if stale:
                    self._desired, desired, dirty = None, None, True
            try:
                self._step(cfg, desired, dirty)
            except Exception as exc:  # noqa: BLE001 — the worker must never die
                self.log.debug("discord_rpc: unexpected worker error: %r", exc, exc_info=True)
                self._drop_connection("disconnected", "Unexpected error: %s" % exc)
        self._shutdown()

    def _wait_timeout(self, cfg):
        if not cfg["enabled"] or not effective_client_id(cfg):
            return None if not self._config_dirty else 0
        if self._config_dirty:
            return 0
        now = self._clock()
        if self._ipc is None:
            return max(0.0, self._next_retry - now)
        if self._dirty:
            return self._bucket.wait_time()
        if self._desired is not None:
            return min(self.IDLE_POLL_S, max(0.0, self._last_submit + STALE_AFTER_S - now) + 0.05)
        return self.IDLE_POLL_S

    def _step(self, cfg, desired, dirty):
        client_id = effective_client_id(cfg)
        if not cfg["enabled"] or not client_id:
            if self._ipc is not None:
                self._send(None)        # wipe what we showed before going quiet
                self._close()
            with self._cond:
                self._dirty = False
            self._applied = _UNSET
            self._set_status("disabled" if not cfg["enabled"] else "no_client_id",
                             "" if not cfg["enabled"] else "Enter your Discord Application ID",
                             endpoint=None, discord_user=None)
            return

        if self._ipc is not None and self._client_id != client_id:
            self._send(None)
            self._close()

        if self._ipc is None:
            if self._clock() < self._next_retry:
                return
            if not self._connect(client_id):
                return
            dirty = True                 # (re)apply desired state on a fresh connection

        if not dirty:
            if not self._ipc.is_alive():
                self._drop_connection("disconnected", "Discord closed the connection")
            return

        if self._applied is not _UNSET and same_activity(self._applied, desired):
            with self._cond:
                if self._desired is desired:
                    self._dirty = False
            return

        wait = self._bucket.wait_time()
        if wait > 0:
            return                       # loop wakes again when a token frees up
        if self._send(desired):
            with self._cond:
                if self._desired is desired:
                    self._dirty = False

    def _connect(self, client_id):
        try:
            ipc = self._ipc_factory(client_id)
            ipc.connect()
        except Exception as exc:  # noqa: BLE001
            if self._rejected and isinstance(exc, self._rejected):
                self._warn_once("Discord rejected the Application ID: %s" % exc)
                self._backoff = self.BACKOFF_MAX_S
                self._set_status("error", "Discord rejected the Application ID (%s)" % exc)
            else:
                self.log.debug("discord_rpc: connect failed: %s", exc)
                self._set_status("disconnected", "Discord desktop app not detected")
            self._next_retry = self._clock() + self._backoff
            self._backoff = min(self._backoff * 2, self.BACKOFF_MAX_S)
            return False
        self._ipc, self._client_id = ipc, client_id
        self._backoff = self.BACKOFF_MIN_S
        # A fresh connection shows nothing yet, so a desired "cleared" state
        # needs no send (saves a rate-limit token and avoids a needless clear).
        self._applied = None
        self._last_warning = None
        user = getattr(ipc, "user", None)
        name = (user.get("global_name") or user.get("username")) if isinstance(user, dict) else None
        self._set_status("connected", "", endpoint=getattr(ipc, "endpoint", None), discord_user=name)
        return True

    def _send(self, activity):
        if self._ipc is None:
            return False
        wire = self._downgrade(activity)
        try:
            self._bucket.take()
            for attempt in range(3):
                try:
                    self._ipc.set_activity(wire)
                    break
                except Exception as exc:  # noqa: BLE001
                    # Older Discord clients reject newer fields over RPC
                    # (activity.type, image URLs). Drop the newest feature still
                    # present, remember that for the session, and retry.
                    rejected = self._rejected and isinstance(exc, self._rejected)
                    if not rejected or attempt == 2 or not self._drop_feature(wire, exc):
                        raise
                    wire = self._downgrade(activity)
                    self._bucket.take()
        except Exception as exc:  # noqa: BLE001
            if self._rejected and isinstance(exc, self._rejected):
                # Discord answered but refused this payload: don't retry it in a loop.
                self._warn_once("Discord rejected the activity: %s" % exc)
                with self._cond:
                    self._status["message"] = "Discord rejected the update (%s)" % exc
                    if self._desired is activity:
                        self._dirty = False
                return False
            self.log.debug("discord_rpc: send failed: %s", exc)
            self._drop_connection("disconnected", "Lost connection to Discord")
            return False
        self._applied = activity
        with self._cond:
            self._status["last_activity"] = wire
            self._status["last_update_at"] = self._wall()
            if self._status["state"] == "connected":
                self._status["message"] = ""
        return True

    @staticmethod
    def _has_url_image(act):
        return bool(act) and str((act.get("assets") or {}).get("large_image", "")).startswith("http")

    def _downgrade(self, activity):
        """Strip features this Discord client has rejected before."""
        if activity is None:
            return None
        wire = activity
        if "type" in wire and not self._type_supported:
            wire = {k: v for k, v in wire.items() if k != "type"}
        if not self._urls_supported and self._has_url_image(wire):
            wire = dict(wire)
            wire["assets"] = dict(wire["assets"], large_image=LARGE_IMAGE_KEY, large_text=LARGE_IMAGE_TEXT)
        return wire

    def _drop_feature(self, wire, exc):
        if wire is None:
            return False
        if self._has_url_image(wire) and self._urls_supported:
            self._urls_supported = False
            self._warn_once("Discord rejected album-cover images; showing the logo instead (%s)" % exc)
            return True
        if "type" in wire and self._type_supported:
            self._type_supported = False
            self._warn_once("Discord rejected the progress-bar activity type; using 'Playing' instead (%s)" % exc)
            return True
        return False

    def _drop_connection(self, state, message):
        self._close()
        self._next_retry = self._clock() + self.BACKOFF_MIN_S
        with self._cond:
            self._dirty = True           # re-send desired state after reconnecting
        self._set_status(state, message, endpoint=None)

    def _close(self):
        ipc, self._ipc = self._ipc, None
        self._applied = _UNSET
        if ipc is not None:
            try:
                ipc.close()
            except Exception:  # noqa: BLE001
                pass

    def _shutdown(self):
        if self._ipc is not None:
            try:
                self._ipc.set_activity(None)
            except Exception:  # noqa: BLE001
                pass
        self._close()

    def _warn_once(self, message):
        if message != self._last_warning:
            self._last_warning = message
            self.log.warning("discord_rpc: %s", message)
