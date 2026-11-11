# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the Discord Rich Presence plugin.

Run from the repo root:  python -m pytest plugins/discord_rpc/tests -q

A fake Discord client listens on a Unix socket in a temp $XDG_RUNTIME_DIR,
so the real IPC bridge is exercised end-to-end without Discord installed.
(The socket tests are skipped on Windows.)
"""

import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location("discord_rpc_test_" + name, PLUGIN_DIR / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ipc = _load("discord_ipc")
presence = _load("presence")

unix_only = pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform.startswith("win"),
                               reason="Unix-socket fake Discord")


# ── fake Discord ────────────────────────────────────────────────────────────

class FakeDiscord:
    VALID_ID = "123456789012345678"

    def __init__(self, directory):
        self.path = os.path.join(directory, "discord-ipc-0")
        self.activities = []          # every SET_ACTIVITY activity (None = clear)
        self.reject_type = False      # simulate an older client that refuses activity.type
        self.reject_url = False       # ...or one that refuses image URLs
        self.received = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self._stop = False
        self._conns = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _read_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _frame(self, conn, op, payload):
        body = json.dumps(payload).encode()
        conn.sendall(struct.pack("<II", op, len(body)) + body)

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self._conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            while True:
                op, length = struct.unpack("<II", self._read_exact(conn, 8))
                data = json.loads(self._read_exact(conn, length) or b"{}")
                if op == 0:
                    if data.get("client_id") != self.VALID_ID:
                        self._frame(conn, 2, {"code": 4000, "message": "Invalid Client ID"})
                        conn.close()
                        return
                    self._frame(conn, 1, {"cmd": "DISPATCH", "evt": "READY",
                                          "data": {"user": {"username": "tester"}}})
                elif op == 1 and data.get("cmd") == "SET_ACTIVITY":
                    activity = data["args"].get("activity")
                    url_image = str(((activity or {}).get("assets") or {}).get("large_image", "")).startswith("http")
                    if (self.reject_type and activity and "type" in activity) or (self.reject_url and url_image):
                        self._frame(conn, 1, {"cmd": "SET_ACTIVITY", "evt": "ERROR", "nonce": data["nonce"],
                                              "data": {"code": 4000, "message": "child \"type\" fails"}})
                        continue
                    self.activities.append(activity)
                    self.received.set()
                    self._frame(conn, 1, {"cmd": "SET_ACTIVITY", "evt": None,
                                          "nonce": data["nonce"], "data": activity})
                elif op == 2:
                    conn.close()
                    return
        except (ConnectionError, OSError, ValueError):
            pass

    def wait_for(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while len(self.activities) < count and time.time() < deadline:
            time.sleep(0.02)
        return len(self.activities) >= count

    def close(self):
        self._stop = True
        for c in self._conns:
            try:
                c.shutdown(socket.SHUT_RDWR)  # a bare close() from another thread may not send EOF
                c.close()
            except OSError:
                pass
        self._srv.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


@pytest.fixture
def runtime_dir(monkeypatch):
    # Short path: AF_UNIX paths are limited to ~104 bytes on macOS.
    d = tempfile.mkdtemp(prefix="drpc")
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    yield d


@pytest.fixture
def discord(runtime_dir):
    fake = FakeDiscord(runtime_dir)
    yield fake
    fake.close()


# ── activity mapping ───────────────────────────────────────────────────────

NOW = 1_800_000_000.0


def test_activity_matches_spec_fields():
    act = presence.build_activity({
        "songTitle": "Hysteria", "artist": "Muse", "instrument": "Bass",
        "score": 12345, "accuracy": 98.4, "duration": 227, "currentTime": 27,
    }, now=NOW, timer_style="bar")
    assert act["details"] == "Hysteria by Muse"
    assert act["state"] == "Bass \u2022 12,345 pts (98.4%)"
    assert act["assets"] == {"large_image": "app_logo", "large_text": "Fee[dB]ack",
                             "small_image": "bass_icon", "small_text": "Bass \u2022 3:47"}
    assert act["timestamps"] == {"start": int((NOW - 27) * 1000), "end": int((NOW + 200) * 1000)}
    assert act["type"] == presence.ACTIVITY_LISTENING      # progress bar "0:27 ━━── 3:47"


@pytest.mark.parametrize("name,key", [
    ("Lead Guitar", "guitar_icon"), ("Rhythm Guitar", "guitar_icon"), ("Bass", "bass_icon"),
    ("Drums", "drums_icon"), ("Keys", "keys_icon"), ("Something Else", "guitar_icon"),
])
def test_instrument_icon_keys(name, key):
    assert presence.resolve_instrument(name) == (name, key)


def test_explicit_icon_key_is_validated():
    assert presence.resolve_instrument("Bass", "custom_icon")[1] == "custom_icon"
    assert presence.resolve_instrument("Bass", "../evil")[1] == "bass_icon"


def test_state_without_score_and_with_toggle_off():
    base = {"songTitle": "S", "artist": "", "instrument": "Drums", "score": 10, "accuracy": 50}
    assert presence.build_activity(dict(base, score=None), now=NOW)["state"] == "Drums"
    assert presence.build_activity(base, now=NOW, show_score=False)["state"] == "Drums"
    # one-char title padded to Discord's 2-char minimum; no "by" without artist
    assert len(presence.build_activity(base, now=NOW)["details"]) == 2


def test_long_text_is_truncated_to_discord_limit():
    act = presence.build_activity({"songTitle": "x" * 300, "artist": "y"}, now=NOW)
    assert len(act["details"]) == 128 and act["details"].endswith("\u2026")


def test_timestamps_scale_with_playback_rate_and_can_be_disabled():
    act = presence.build_activity({"duration": 100, "currentTime": 10, "playbackRate": 0.5}, now=NOW,
                                  timer_style="bar")
    assert act["timestamps"] == {"start": int((NOW - 20) * 1000), "end": int((NOW + 180) * 1000)}
    assert "timestamps" not in presence.build_activity({"currentTime": 10}, now=NOW, show_timestamps=False)


def test_same_activity_ignores_small_timestamp_drift():
    a = presence.build_activity({"songTitle": "A", "currentTime": 10, "duration": 60}, now=NOW)
    b = presence.build_activity({"songTitle": "A", "currentTime": 14, "duration": 60}, now=NOW + 4.5)
    c = presence.build_activity({"songTitle": "A", "currentTime": 40, "duration": 60}, now=NOW + 4)
    assert presence.same_activity(a, b)
    assert not presence.same_activity(a, c)
    assert presence.same_activity(None, None) and not presence.same_activity(a, None)


# ── config ─────────────────────────────────────────────────────────────────

def test_config_store_fails_closed(tmp_path):
    store = presence.ConfigStore(tmp_path / "discord_rpc.json")
    assert store.read() == presence.DEFAULT_CONFIG
    (tmp_path / "discord_rpc.json").write_text('{"enabled": "true", "client_id": "abc"}')
    assert store.read()["enabled"] is False and store.read()["client_id"] == ""
    with pytest.raises(ValueError):
        store.update({"client_id": "not-a-number"})
    cfg = store.update({"enabled": True, "client_id": FakeDiscord.VALID_ID})
    assert cfg["enabled"] is True and store.read()["client_id"] == FakeDiscord.VALID_ID


# ── IPC bridge ─────────────────────────────────────────────────────────────

def test_no_discord_raises_not_found(runtime_dir):
    with pytest.raises(ipc.DiscordNotFound):
        ipc.DiscordIPC(FakeDiscord.VALID_ID, timeout=0.5).connect()


def test_windows_pipe_paths():
    paths = ipc.candidate_paths("win32", {})
    assert paths[0] == r"\\.\pipe\discord-ipc-0" and len(paths) == 10


@unix_only
def test_ipc_handshake_set_and_clear(discord):
    client = ipc.DiscordIPC(FakeDiscord.VALID_ID, timeout=2)
    client.connect()
    assert client.user == {"username": "tester"}
    client.set_activity({"details": "Song by Artist"})
    client.clear_activity()
    client.close()
    assert discord.activities == [{"details": "Song by Artist"}, None]


@unix_only
def test_ipc_invalid_client_id(discord):
    with pytest.raises(ipc.DiscordRPCError) as err:
        ipc.DiscordIPC("999999999999999999", timeout=2).connect()
    assert err.value.code == 4000


# ── worker ─────────────────────────────────────────────────────────────────

def _service(tmp_path, enabled=True):
    store = presence.ConfigStore(tmp_path / "discord_rpc.json")
    store.update({"enabled": enabled, "client_id": FakeDiscord.VALID_ID})
    svc = presence.PresenceService(store, lambda cid: ipc.DiscordIPC(cid, timeout=2),
                                   rejected_errors=(ipc.DiscordRPCError,))
    svc.start()
    return svc


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@unix_only
def test_service_publishes_dedupes_and_clears(discord, tmp_path):
    svc = _service(tmp_path)
    try:
        act = presence.build_activity({"songTitle": "A", "artist": "B", "instrument": "Bass",
                                       "currentTime": 5, "duration": 60})
        svc.submit(act)
        assert discord.wait_for(1)
        assert discord.activities[0]["details"] == "A by B (1:00)"
        assert _wait(lambda: svc.status()["state"] == "connected")
        assert svc.status()["discord_user"] == "tester"

        svc.submit(dict(act))              # identical → must not cost a Discord update
        time.sleep(0.3)
        assert len(discord.activities) == 1

        svc.clear()
        assert discord.wait_for(2) and discord.activities[-1] is None
        assert _wait(lambda: svc.status()["last_activity"] is None)
    finally:
        svc.stop()


@unix_only
def test_service_disable_clears_presence(discord, tmp_path):
    svc = _service(tmp_path)
    try:
        svc.submit(presence.build_activity({"songTitle": "Song"}))
        assert discord.wait_for(1)
        svc.store.update({"enabled": False})
        svc.reconfigure()
        assert discord.wait_for(2) and discord.activities[-1] is None
        assert _wait(lambda: svc.status()["state"] == "disabled")
    finally:
        svc.stop()


def test_service_survives_discord_being_closed(runtime_dir, tmp_path):
    svc = _service(tmp_path)
    try:
        svc.submit(presence.build_activity({"songTitle": "Song"}))
        assert _wait(lambda: svc.status()["state"] == "disconnected")
        assert svc._thread.is_alive()
    finally:
        svc.stop()


@unix_only
def test_service_reconnects_when_discord_starts_later(runtime_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(presence.PresenceService, "BACKOFF_MIN_S", 0.1)
    svc = _service(tmp_path)
    fake = None
    try:
        svc.submit(presence.build_activity({"songTitle": "Late"}))
        assert _wait(lambda: svc.status()["state"] == "disconnected")
        fake = FakeDiscord(runtime_dir)
        assert fake.wait_for(1, timeout=8)
        assert fake.activities[0]["details"].startswith("Late")
    finally:
        svc.stop()
        if fake:
            fake.close()


def test_rate_limit_bucket():
    clock = [0.0]
    bucket = presence._TokenBucket(5, 20.0, lambda: clock[0])
    for _ in range(5):
        assert bucket.wait_time() == 0
        bucket.take()
    assert bucket.wait_time() == pytest.approx(20.0)
    clock[0] = 20.01
    assert bucket.wait_time() == 0


# ── HTTP routes (needs FastAPI; runs in the Fee[dB]ack dev env) ─────────────

def test_routes_error_boundaries(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    routes = _load("routes")
    app = FastAPI()
    routes.setup(app, {"config_dir": str(tmp_path), "load_sibling": lambda n: {"discord_ipc": ipc, "presence": presence}[n]})
    client = TestClient(app)
    try:
        r = client.post("/api/plugins/discord_rpc/presence", json={
            "songTitle": "Hysteria", "artist": "Muse", "instrument": "Bass",
            "score": 1000, "accuracy": 98.4, "duration": 200, "currentTime": 10})
        body = r.json()
        assert r.status_code == 200 and body["ok"] and body["broadcast"] is False  # opt-in default
        assert body["activity"]["state"] == "Bass \u2022 1,000 pts (98.4%)"
        assert client.post("/api/plugins/discord_rpc/clear").json()["ok"]
        assert client.post("/api/plugins/discord_rpc/config", json={"client_id": "nope"}).status_code == 400
        cfg = client.post("/api/plugins/discord_rpc/config",
                          json={"enabled": True, "client_id": FakeDiscord.VALID_ID}).json()
        assert cfg["enabled"] is True
        assert client.get("/api/plugins/discord_rpc/status").json()["ok"]
        # second setup() must not duplicate routes or leak a second worker
        routes.setup(app, {"config_dir": str(tmp_path), "load_sibling": lambda n: {"discord_ipc": ipc, "presence": presence}[n]})
        assert sum(1 for rt in app.routes if getattr(rt, "path", "") == "/api/plugins/discord_rpc/presence") == 1
    finally:
        app.state.discord_rpc_service.stop()


@unix_only
def test_service_notices_discord_quitting_while_idle(runtime_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(presence.PresenceService, "IDLE_POLL_S", 0.2)
    monkeypatch.setattr(presence.PresenceService, "BACKOFF_MIN_S", 0.2)
    fake = FakeDiscord(runtime_dir)
    svc = _service(tmp_path)
    try:
        svc.submit(presence.build_activity({"songTitle": "A"}))
        assert fake.wait_for(1)
        fake.close()
        assert _wait(lambda: svc.status()["state"] == "disconnected")
        fake = FakeDiscord(runtime_dir)        # Discord restarts → desired state re-applied
        assert fake.wait_for(1, timeout=8)
    finally:
        svc.stop()
        fake.close()


def test_no_route_collides_with_core_plugin_routes():
    """Core registers these BEFORE plugins load, so they shadow plugin routes
    (feedBack plugins/__init__.py register_plugin_api). A POST to
    /api/plugins/discord_rpc/update would run core's git self-updater."""
    import re
    core_post = {"update", "enabled"}
    core_get = {"screen.html", "screen.js", "settings.html", "tour.json"}
    src = (PLUGIN_DIR / "routes.py").read_text()
    posts = set(re.findall(r'@router\.post\("/([^"]+)"\)', src))
    gets = set(re.findall(r'@router\.get\("/([^"]+)"\)', src))
    assert posts and not posts & core_post
    assert not gets & core_get
    assert not any(p.startswith(("assets/", "src/")) for p in posts | gets)
    js = (PLUGIN_DIR / "screen.js").read_text()
    for path in re.findall(r"request\('/([a-z_]+)'", js):
        assert path in posts | gets, path


def test_builtin_application_id_is_default(tmp_path):
    store = presence.ConfigStore(tmp_path / "discord_rpc.json")
    assert presence.CLIENT_ID_RE.match(presence.DEFAULT_CLIENT_ID)
    assert presence.effective_client_id(store.read()) == presence.DEFAULT_CLIENT_ID
    store.update({"client_id": FakeDiscord.VALID_ID})
    assert presence.effective_client_id(store.read()) == FakeDiscord.VALID_ID
    store.update({"client_id": ""})            # clearing the override restores the official app
    assert presence.effective_client_id(store.read()) == presence.DEFAULT_CLIENT_ID


def test_builtin_application_id_connects_without_user_setup(runtime_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeDiscord, "VALID_ID", presence.DEFAULT_CLIENT_ID)
    if not hasattr(socket, "AF_UNIX") or sys.platform.startswith("win"):
        pytest.skip("Unix-socket fake Discord")
    fake = FakeDiscord(runtime_dir)
    store = presence.ConfigStore(tmp_path / "discord_rpc.json")
    store.update({"enabled": True})            # the only thing a user has to do
    svc = presence.PresenceService(store, lambda cid: ipc.DiscordIPC(cid, timeout=2),
                                   rejected_errors=(ipc.DiscordRPCError,))
    svc.start()
    try:
        svc.submit(presence.build_activity({"songTitle": "Hidden in the Sand", "artist": "Tally Hall"}))
        assert fake.wait_for(1)
        assert svc.status()["using_default_app"] is True
    finally:
        svc.stop()
        fake.close()


def test_tuning_is_shown_and_can_be_turned_off():
    base = {"songTitle": "Hidden in the Sand", "artist": "Tally Hall", "instrument": "Bass Guitar",
            "tuning": "E Standard", "score": 12345, "accuracy": 98.4}
    full = presence.build_activity(base, now=NOW)
    assert full["state"] == "Bass Guitar \u2022 12,345 pts (98.4%)"          # tuning doesn't fit the line...
    assert full["assets"]["small_text"] == "Bass Guitar \u2022 E Standard"   # ...so it lives in the hover
    assert presence.build_activity(base, now=NOW, show_tuning=False)["assets"]["small_text"] == "Bass Guitar"
    assert presence.build_activity(dict(base, score=None), now=NOW)["state"] == "Bass Guitar \u2022 E Standard"
    assert presence.build_activity(dict(base, tuning=""), now=NOW, show_score=False)["state"] == "Bass Guitar"


def test_bass_is_called_bass_guitar():
    assert presence.resolve_instrument("") == ("Guitar", "guitar_icon")
    assert presence._INSTRUMENTS[0][1] == "Bass Guitar"
    act = presence.build_activity({"instrument": "Bass Guitar"}, now=NOW)
    assert act["assets"]["small_image"] == "bass_icon"
    assert act["assets"]["small_text"] == "Bass Guitar"


def test_no_progress_bar_without_end_time():
    act = presence.build_activity({"currentTime": 5}, now=NOW)        # unknown duration
    assert "type" not in act and act["timestamps"]["start"]
    assert "type" not in presence.build_activity({"currentTime": 5, "duration": 60}, now=NOW, show_timestamps=False)


@unix_only
def test_falls_back_to_playing_when_discord_rejects_type(discord, tmp_path):
    discord.reject_type = True
    svc = _service(tmp_path)
    try:
        act = presence.build_activity({"songTitle": "Song", "currentTime": 5, "duration": 60}, timer_style="bar")
        assert act["type"] == presence.ACTIVITY_LISTENING
        svc.submit(act)
        assert discord.wait_for(1)
        assert "type" not in discord.activities[0] and discord.activities[0]["timestamps"]
        assert svc.status()["state"] == "connected"
        svc.submit(presence.build_activity({"songTitle": "Next", "currentTime": 1, "duration": 60}, timer_style="bar"))
        assert discord.wait_for(2) and "type" not in discord.activities[1]   # remembered: no retry dance
    finally:
        svc.stop()


def test_browsing_presence_shows_screen_and_session_timer():
    act = presence.build_activity({"mode": "browsing", "activityLabel": "Browsing the song library",
                                   "sessionStart": NOW - 600}, now=NOW)
    assert act["details"] == "Browsing the song library"
    assert "state" not in act and "type" not in act
    assert act["assets"] == {"large_image": "app_logo", "large_text": "Fee[dB]ack"}
    assert act["timestamps"] == {"start": int((NOW - 600) * 1000)}     # "10:00 elapsed"
    assert presence.build_activity({"mode": "browsing"}, now=NOW)["details"] == "Browsing the menus"
    assert "timestamps" not in presence.build_activity({"mode": "browsing", "sessionStart": NOW}, now=NOW,
                                                       show_timestamps=False)


def test_paused_presence_keeps_song_but_no_timer():
    act = presence.build_activity({"mode": "paused", "songTitle": "Hidden in the Sand", "artist": "Tally Hall",
                                   "instrument": "Bass Guitar", "tuning": "E Standard",
                                   "currentTime": 30, "duration": 117}, now=NOW)
    assert act["details"] == "Hidden in the Sand by Tally Hall"
    assert act["state"] == "Paused \u2022 Bass Guitar"                      # + tuning would exceed 32
    assert act["assets"]["small_text"] == "Bass Guitar \u2022 E Standard \u2022 1:57"
    assert "timestamps" not in act and "type" not in act


def test_show_idle_defaults_on_and_is_a_strict_bool(tmp_path):
    store = presence.ConfigStore(tmp_path / "discord_rpc.json")
    assert store.read()["show_idle"] is True
    assert store.update({"show_idle": "false"})["show_idle"] is True   # strings never toggle settings
    assert store.update({"show_idle": False})["show_idle"] is False


def test_default_timer_counts_up_and_keeps_playing_header():
    act = presence.build_activity({"songTitle": "Hidden in the Sand", "artist": "Tally Hall",
                                   "instrument": "Bass Guitar", "currentTime": 15, "duration": 117.9}, now=NOW)
    assert "type" not in act                                   # → "Playing Fee[dB]ack"
    assert act["timestamps"] == {"start": int((NOW - 15) * 1000)}   # → "0:15 elapsed"
    assert act["details"] == "Hidden in the Sand by Tally Hall"         # 32 chars: no room for "(1:58)"
    assert act["assets"]["small_text"] == "Bass Guitar \u2022 1:58"           # ...so the length is in the hover
    short = presence.build_activity({"songTitle": "Sugar", "artist": "Maroon 5", "currentTime": 1,
                                     "duration": 235}, now=NOW)
    assert short["details"] == "Sugar by Maroon 5 (3:55)"
    assert presence.format_length(3725) == "1:02:05"
    # no length suffix when the timer is off, when paused, or when the bar shows it
    assert presence.build_activity({"songTitle": "S", "duration": 60, "currentTime": 1}, now=NOW,
                                   show_timestamps=False)["details"] == "S\u200b"
    assert presence.build_activity({"songTitle": "Song", "duration": 60, "currentTime": 1}, now=NOW,
                                   timer_style="bar")["details"] == "Song"


def test_long_title_is_cut_and_length_kept_in_hover():
    act = presence.build_activity({"songTitle": "x" * 300, "duration": 200, "currentTime": 1}, now=NOW)
    assert len(act["details"]) == 128 and act["details"].endswith("\u2026")
    assert act["assets"]["small_text"].endswith("3:20")


def test_timer_style_config_is_validated(tmp_path):
    store = presence.ConfigStore(tmp_path / "discord_rpc.json")
    assert store.read()["timer_style"] == "elapsed"
    assert store.update({"timer_style": "bar"})["timer_style"] == "bar"
    assert store.update({"timer_style": "sparkles"})["timer_style"] == "bar"   # unknown values ignored


def test_compact_lines_fit_and_never_drop_the_score():
    act = presence.build_activity({"songTitle": "Song", "artist": "Band", "instrument": "Rhythm Guitar",
                                   "tuning": "Drop D", "score": 950, "accuracy": 87}, now=NOW)
    assert act["state"] == "Rhythm Guitar \u2022 950 pts (87%)"    # "87%" not "87.0%"
    assert len(act["state"]) <= presence.COMPACT_WIDTH
    act = presence.build_activity({"songTitle": "Song", "instrument": "Bass Guitar", "tuning": "Drop D"}, now=NOW)
    assert act["state"] == "Bass Guitar \u2022 Drop D"             # room for tuning when there's no score yet


class _FakeDB:
    def __init__(self, row):
        self.row = row

    def get_enrichment(self, filename):
        return self.row


RID = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"


def test_cover_used_when_feedback_fetched_it():
    db = _FakeDB({"match_state": "matched", "mb_release_id": RID, "art_state": "caa",
                  "canon_album": "Marvin's Marvelous Mechanical Museum", "canon_year": "2005"})
    cover = presence.CoverResolver(db, checker=lambda rid: pytest.fail("no network needed")).lookup("song.sloppak")
    assert cover == {"url": "https://coverartarchive.org/release/%s/front-250" % RID,
                     "text": "Marvin's Marvelous Mechanical Museum (2005)"}
    act = presence.build_activity({"songTitle": "S"}, now=NOW, cover=cover)
    assert act["assets"]["large_image"].startswith("https://coverartarchive.org/")
    assert act["assets"]["small_image"] == "guitar_icon"                 # instrument badge stays


@pytest.mark.parametrize("row", [
    None,
    {"match_state": "unmatched", "mb_release_id": RID, "art_state": "caa"},
    {"match_state": "matched", "mb_release_id": RID, "art_state": "none"},
    {"match_state": "matched", "mb_release_id": "not-a-uuid", "art_state": "caa"},
])
def test_no_cover_without_a_known_release(row):
    assert presence.CoverResolver(_FakeDB(row), checker=lambda rid: True).lookup("x") is None
    assert presence.CoverResolver(None).lookup("x") is None


def test_unverified_cover_is_checked_once_in_background(monkeypatch):
    monkeypatch.delenv("FEEDBACK_ENRICH_OFFLINE", raising=False)
    monkeypatch.delenv("FEEDBACK_SKIP_STARTUP_TASKS", raising=False)
    calls = []
    gate = threading.Event()

    def checker(rid):
        calls.append(rid)
        gate.wait(2)
        return True

    res = presence.CoverResolver(_FakeDB({"match_state": "manual", "mb_release_id": RID, "art_state": None}),
                                 checker=checker)
    assert res.lookup("x") is None            # never blocks: logo until verified
    assert res.lookup("x") is None            # still pending - no second check
    gate.set()
    assert _wait(lambda: res.lookup("x") is not None)
    assert calls == [RID]


def test_offline_switch_blocks_cover_checks(monkeypatch):
    monkeypatch.setenv("FEEDBACK_ENRICH_OFFLINE", "1")
    res = presence.CoverResolver(_FakeDB({"match_state": "matched", "mb_release_id": RID, "art_state": None}),
                                 checker=lambda rid: pytest.fail("must not touch the network"))
    assert res.lookup("x") is None


@unix_only
def test_falls_back_to_logo_when_discord_rejects_image_urls(discord, tmp_path):
    discord.reject_url = True                 # refuse any activity whose large_image is a URL
    svc = _service(tmp_path)
    try:
        cover = {"url": "https://coverartarchive.org/release/%s/front-250" % RID, "text": "Album"}
        svc.submit(presence.build_activity({"songTitle": "Song"}, cover=cover))
        assert discord.wait_for(1)
        assets = discord.activities[0]["assets"]
        assert assets["large_image"] == "app_logo" and assets["large_text"] == "Fee[dB]ack"
        assert svc.status()["state"] == "connected"
    finally:
        svc.stop()
