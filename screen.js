/*
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (C) 2026 Fee[dB]ack contributors
 *
 * This program is free software: you can redistribute it and/or modify it
 * under the terms of the GNU Affero General Public License as published by the
 * Free Software Foundation, version 3 of the License. It is distributed WITHOUT
 * ANY WARRANTY; see <https://www.gnu.org/licenses/> for details.
 *
 * Discord Rich Presence — frontend.
 *
 * Listens to the host event bus (window.feedBack, an EventTarget; payloads in
 * event.detail) and keeps the backend's Discord activity in sync:
 *
 *   requested hook   host event(s) actually used
 *   ─────────────    ──────────────────────────────────────────────
 *   onSongStart      song:loaded (metadata), song:play / song:resume
 *   onSongTick       song:position-changed (≤4 Hz) + song:seek
 *   onSongPause      song:pause
 *   onSongEnd        song:ended, song:stop, screen:changed (leaving the player)
 *
 * While playing, state is POSTed to /presence every UPDATE_INTERVAL_MS (4 s — inside the
 * 3–5 s window; the backend additionally rate-limits to Discord's 5 per 20 s
 * and drops no-op updates). Pause / stop / menu navigation clear immediately.
 *
 * Live score: core has no score event — scoring belongs to scorer plugins
 * (e.g. note_detect). Any scorer can feed this plugin with either
 *     window.feedBack.emit('discord_rpc:score', { score: 12345, accuracy: 98.4 })
 *     window.feedBack.discordRpc.reportScore({ score, accuracy })
 * where accuracy is a percentage (0–100); pass { accuracyFraction: 0.984 }
 * instead if you track 0–1. Without a scorer the presence shows instrument only.
 */
(function () {
    'use strict';

    var API = '/api/plugins/discord_rpc';
    var SCREEN_ID = 'plugin-discord_rpc';
    var PLAYER_SCREEN_ID = 'player';
    var UPDATE_INTERVAL_MS = 4000;
    var SEEK_DEBOUNCE_MS = 600;
    var STATUS_POLL_MS = 5000;
    var REQUEST_TIMEOUT_MS = 4000;
    var IDLE_INTERVAL_MS = 15000;      // heartbeat while browsing/paused (backend auto-clears after 30 s of silence)
    var SESSION_START = Date.now() / 1000;

    // What friends see while you're in the menus (screen id → label).
    var SCREEN_LABELS = {
        'home': 'Browsing the song library',
        'favorites': 'Browsing the song library',
        'v3-songs': 'Browsing the song library',
        'v3-saved': 'Browsing the song library',
        'v3-home': 'On the home screen',
        'v3-playlists': 'Browsing playlists',
        'v3-lessons': 'Checking out lessons',
        'v3-progress': 'Looking at their progress',
        'v3-profile': 'Looking at their profile',
        'v3-shop': 'Browsing the shop',
        'v3-feedbarcade': 'In the Feedbarcade',
        'v3-plugins': 'Browsing plugins',
        'settings': 'Tweaking settings',
        'plugin-career': 'In Career mode',
        'plugin-minigames': 'Playing minigames',
        'plugin-tutorials': 'Doing a tutorial',
        'player': 'Loading a song',                 // player open, chart still loading
    };
    var DEFAULT_SCREEN_LABEL = 'Browsing the menus';
    var LOG_PREFIX = '[discord_rpc]';

    var bus = window.feedBack;
    if (!bus || typeof bus.on !== 'function') return;

    // Re-loads (plugin update / re-enable) must not stack listeners or timers.
    if (bus.discordRpc && typeof bus.discordRpc.destroy === 'function') {
        try { bus.discordRpc.destroy(); } catch (_) { /* best effort */ }
    }

    // ── State ───────────────────────────────────────────────────────────────
    var song = null;          // { title, artist, instrument, instrumentKey, duration }
    var playing = false;
    var position = { time: 0, duration: null };
    var score = { score: null, accuracy: null };
    var OPTION_KEYS = ['show_score', 'show_tuning', 'show_timestamps', 'show_idle', 'show_cover'];
    var cfg = { enabled: false, client_id: '', show_score: true, show_tuning: true, show_timestamps: true, show_idle: true,
                show_cover: true,
                timer_style: 'elapsed' };
    var currentScreen = null;
    var panelVisible = false;
    var idleCleared = false;   // show_idle off: whether the "nothing playing" clear was already sent
    var view = { status: null, activity: null, broadcast: false };
    var timers = { update: null, seek: null, poll: null, tick: null };
    var listeners = [];
    var destroyed = false;

    // ── Utilities ───────────────────────────────────────────────────────────
    function warn(err) {
        try { console.warn(LOG_PREFIX, err); } catch (_) { /* noop */ }
    }

    /**
     * Subscribe with an error boundary. Bus handlers receive event.detail;
     * DOM handlers receive the raw Event. Everything is tracked for destroy().
     */
    function listen(target, name, fn) {
        if (!target) return;
        var isBus = target === bus;
        var wrapped = function (event) {
            if (destroyed) return;
            try { fn(isBus ? (event && event.detail) : event); } catch (err) { warn(err); }
        };
        if (isBus) bus.on(name, wrapped); else target.addEventListener(name, wrapped);
        listeners.push([target, name, wrapped]);
    }

    function finite(n) {
        var v = Number(n);
        return (n === null || n === undefined || n === '' || !isFinite(v)) ? null : v;
    }

    /** fetch → parsed JSON or null. Never rejects. */
    function request(path, body) {
        var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
        var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS) : null;
        var opts = { method: body === undefined ? 'GET' : 'POST', signal: ctrl ? ctrl.signal : undefined };
        if (body !== undefined) {
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(body);
        }
        return fetch(API + path, opts)
            .then(function (r) { return r.json().catch(function () { return null; }); })
            .catch(function () { return null; })
            .then(function (data) { if (timer) clearTimeout(timer); return data; });
    }

    // ── Metadata extraction ─────────────────────────────────────────────────
    var INSTRUMENTS = [
        ['bass', 'Bass Guitar', 'bass_icon'],
        ['drum', 'Drums', 'drums_icon'],
        ['key', 'Keys', 'keys_icon'],
        ['piano', 'Keys', 'keys_icon'],
        ['vocal', 'Vocals', 'vocals_icon'],
        ['lead', 'Lead Guitar', 'guitar_icon'],
        ['rhythm', 'Rhythm Guitar', 'guitar_icon'],
        ['combo', 'Combo Guitar', 'guitar_icon'],
        ['guitar', 'Guitar', 'guitar_icon'],
    ];

    function resolveInstrument(arrangement, smartName) {
        var raw = String(arrangement || smartName || '').trim();
        var lowered = (raw + ' ' + String(smartName || '')).toLowerCase();
        for (var i = 0; i < INSTRUMENTS.length; i++) {
            if (lowered.indexOf(INSTRUMENTS[i][0]) !== -1) {
                return { name: INSTRUMENTS[i][1], key: INSTRUMENTS[i][2] };
            }
        }
        return { name: raw || 'Guitar', key: 'guitar_icon' };
    }

    /** Human tuning name ("E Standard", "Drop D") via core's own helper. */
    function tuningName(raw) {
        var namer = bus.displayTuningName || window.displayTuningName;
        if (typeof namer !== 'function' || raw == null) return '';
        try {
            return String(Array.isArray(raw) ? namer(null, raw) : namer(String(raw), null) || '').trim();
        } catch (_) { return ''; }
    }

    function readSong(info) {
        info = info || bus.currentSong;
        if (!info) return null;
        var inst = resolveInstrument(info.arrangement, info.arrangementSmartName);
        return {
            filename: info.filename ? String(info.filename) : null,
            title: String(info.title || '').trim(),
            tuning: tuningName(info.tuning),
            artist: String(info.artist || '').trim(),
            instrument: inst.name,
            instrumentKey: inst.key,
            duration: finite(info.duration),
        };
    }

    function audioClock() {
        // Read the live clock rather than extrapolating from the last event.
        try {
            if (window._juceMode && window.jucePlayer) {
                return { time: finite(window.jucePlayer.currentTime), duration: finite(window.jucePlayer.duration),
                         rate: finite(window.jucePlayer._speed) || 1 };
            }
            var el = document.getElementById('audio');
            if (el) return { time: finite(el.currentTime), duration: finite(el.duration), rate: finite(el.playbackRate) || 1 };
        } catch (_) { /* fall through */ }
        return { time: null, duration: null, rate: 1 };
    }

    /**
     * Pull the live score from the Note Detection plugin
     * (github.com/got-feedback/feedBack-plugin-notedetect), which exposes
     * window.noteDetect.getStats() → { score, accuracy (0–100), ... }.
     * Only trusted while detection is running: when it is off, its stats are
     * stale (previous song) or zero. Called once per 4 s update, so cheap.
     */
    function readNoteDetect() {
        var nd = window.noteDetect;
        if (!nd || typeof nd.getStats !== 'function') return;
        try {
            if (typeof nd.isEnabled === 'function' && !nd.isEnabled()) return;
            var stats = nd.getStats();
            if (!stats) return;
            var hasAttempts = (Number(stats.hits) || 0) + (Number(stats.misses) || 0) > 0;
            if (finite(stats.score) !== null) score.score = finite(stats.score);
            if (hasAttempts && finite(stats.accuracy) !== null) score.accuracy = finite(stats.accuracy);
        } catch (_) { /* a scorer bug must never break presence */ }
    }

    function screenLabel(id) {
        return (id && SCREEN_LABELS[id]) || DEFAULT_SCREEN_LABEL;
    }

    /** 'playing' | 'paused' (song open in the player, not playing) | 'browsing' (menus). */
    function currentMode() {
        if (playing) return 'playing';
        if (currentScreen === PLAYER_SCREEN_ID && (song || bus.currentSong)) return 'paused';
        return 'browsing';
    }

    function buildPayload() {
        var mode = currentMode();
        if (mode !== 'browsing' && !song) song = readSong();
        if (mode === 'browsing' || !song) {
            return { mode: 'browsing', activityLabel: screenLabel(currentScreen), sessionStart: SESSION_START };
        }
        var clock = audioClock();
        var t = clock.time !== null ? clock.time : position.time;
        var d = song.duration || clock.duration || position.duration;
        var payload = {
            mode: mode,
            filename: song.filename || (bus.currentSong && bus.currentSong.filename) || null,
            songTitle: song.title,
            artist: song.artist,
            instrument: song.instrument,
            instrumentKey: song.instrumentKey,
            tuning: song.tuning || null,
            currentTime: t !== null && t >= 0 ? Math.min(t, 86400) : null,
            duration: d && d > 0 ? Math.min(d, 86400) : null,
            playbackRate: Math.min(Math.max(clock.rate || 1, 0.1), 4),
        };
        readNoteDetect();
        if (score.score !== null) payload.score = Math.max(0, score.score);
        if (score.accuracy !== null) payload.accuracy = Math.round(Math.min(Math.max(score.accuracy, 0), 100) * 10) / 10;
        return payload;
    }

    // ── Backend sync ────────────────────────────────────────────────────────
    function sendUpdate() {
        var payload = buildPayload();
        if (!payload) return;
        request('/presence', payload).then(function (res) {
            if (!res || !res.ok || destroyed) return;
            view.activity = res.activity || null;
            view.broadcast = !!res.broadcast;
            if (res.status && view.status) view.status.state = res.status;
            render();
        });
    }

    function sendClear() {
        view.activity = null;
        view.broadcast = false;
        render();
        return request('/clear', {});
    }

    /**
     * Push the presence for the current mode and schedule the next refresh:
     * every 4 s while playing (live score/position), every 15 s otherwise
     * (keeps the backend's 30 s stale-clear from firing). Call it whenever
     * the mode may have changed — it is cheap and the backend drops no-ops.
     */
    function sync() {
        if (timers.update) { clearTimeout(timers.update); timers.update = null; }
        if (destroyed) return;
        var mode = currentMode();
        if (mode !== 'playing' && !cfg.show_idle) {
            if (!idleCleared) { idleCleared = true; sendClear(); }
        } else if (cfg.enabled || panelVisible) {
            idleCleared = false;
            sendUpdate();
        }
        timers.update = setTimeout(sync, mode === 'playing' ? UPDATE_INTERVAL_MS : IDLE_INTERVAL_MS);
    }

    function stopTimers() {
        if (timers.update) { clearTimeout(timers.update); timers.update = null; }
        if (timers.seek) { clearTimeout(timers.seek); timers.seek = null; }
    }

    // ── Event wiring ────────────────────────────────────────────────────────
    listen(bus, 'song:loaded', function (detail) {
        song = readSong(detail);
        score = { score: null, accuracy: null };   // new song / arrangement → fresh score
        position = { time: 0, duration: song ? song.duration : null };
        sync();
    });

    listen(bus, 'song:arrangement-changed', function () {
        song = null;                               // re-read on next song:loaded / update
        score = { score: null, accuracy: null };
    });

    function onPlay(detail) {
        if (detail && finite(detail.time) !== null) position.time = finite(detail.time);
        if (playing) return;                       // song:play + song:resume arrive as a pair
        playing = true;
        sync();
    }
    listen(bus, 'song:play', onPlay);
    listen(bus, 'song:resume', onPlay);

    function onStopped() {
        if (!playing) return;
        playing = false;
        sync();                                    // → "Paused" in the player, or the menu label
    }
    listen(bus, 'song:pause', onStopped);
    listen(bus, 'song:ended', onStopped);
    listen(bus, 'song:stop', onStopped);

    listen(bus, 'song:position-changed', function (detail) {
        if (!detail) return;
        if (finite(detail.time) !== null) position.time = finite(detail.time);
        if (finite(detail.duration) !== null) position.duration = finite(detail.duration);
    });

    listen(bus, 'song:seek', function (detail) {
        if (detail && finite(detail.to) !== null) position.time = finite(detail.to);
        if (!playing) return;
        // Timestamps jumped — refresh soon, debounced so scrubbing isn't a flood.
        if (timers.seek) clearTimeout(timers.seek);
        timers.seek = setTimeout(function () { timers.seek = null; sync(); }, SEEK_DEBOUNCE_MS);
    });

    listen(bus, 'screen:changed', function (detail) {
        var id = detail && detail.id;
        if (!id) return;
        currentScreen = id;
        if (id !== PLAYER_SCREEN_ID) playing = false;   // leaving the player ends playback
        if (id === SCREEN_ID) onPanelShown(); else onPanelHidden();
        sync();
    });

    function reportScore(data) {
        if (!data) return;
        var s = finite(data.score);
        var a = finite(data.accuracy);
        var f = finite(data.accuracyFraction);
        if (a === null && f !== null) a = f * 100;
        if (s !== null) score.score = s;
        if (a !== null) score.accuracy = a;
        // No immediate POST: the next 4 s tick carries it (rate-limit friendly).
    }
    listen(bus, 'discord_rpc:score', reportScore);

    // Tab closed mid-song: best-effort clear (the backend also auto-clears
    // a presence nobody refreshes for 30 s, and Discord clears it when
    // Fee[dB]ack's process exits).
    listen(window, 'pagehide', function () {
        if (!playing && !view.activity) return;
        try {
            if (navigator.sendBeacon) navigator.sendBeacon(API + '/clear', new Blob(['{}'], { type: 'application/json' }));
        } catch (_) { /* noop */ }
    });

    // ── Panel (screen.html) ─────────────────────────────────────────────────
    function $(id) { return document.getElementById(id); }

    var STATUS_TEXT = {
        connected: 'Connected to Discord',
        disconnected: 'Discord Disconnected',
        error: 'Discord rejected the connection',
        disabled: 'Rich Presence is off',
        no_client_id: 'Needs a Discord Application ID',
        unknown: 'Checking…',
    };

    function fmtClock(seconds) {
        seconds = Math.max(0, Math.floor(seconds));
        var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
        var mm = (h ? String(m).padStart(2, '0') : String(m)) + ':' + String(s).padStart(2, '0');
        return h ? h + ':' + mm : mm;
    }

    function setText(id, text) {
        var el = $(id);
        if (el && el.textContent !== text) el.textContent = text;
    }

    var progressEls = null;   // resolved once (no per-tick DOM queries)

    function renderTimer() {
        var act = view.activity;
        var ts = act && act.timestamps;
        if (!progressEls || !progressEls.wrap.isConnected) {
            progressEls = { wrap: $('drpc-preview-progress'), fill: $('drpc-preview-fill') };
        }
        var wrap = progressEls.wrap;
        var now = Date.now();
        if (ts && ts.start && ts.end) {
            // Mirrors Discord's Listening layout: "0:01 ━━━━──── 1:57".
            var total = Math.max(1, ts.end - ts.start);
            var elapsed = Math.min(Math.max(0, now - ts.start), total);
            if (wrap) wrap.hidden = false;
            setText('drpc-preview-elapsed', fmtClock(elapsed / 1000));
            setText('drpc-preview-total', fmtClock(total / 1000));
            if (progressEls.fill) progressEls.fill.style.width = (elapsed / total * 100).toFixed(1) + '%';
            setText('drpc-preview-time', '');
            return;
        }
        if (wrap) wrap.hidden = true;
        setText('drpc-preview-time', ts && ts.start ? fmtClock((now - ts.start) / 1000) + ' elapsed' : '');
    }

    function render() {
        var root = $(SCREEN_ID);
        if (!root || !$('drpc-status')) return;

        var st = (view.status && view.status.state) || 'unknown';
        var pill = $('drpc-status');
        pill.dataset.state = st;
        setText('drpc-status-text', STATUS_TEXT[st] || st);
        var detail = view.status && (view.status.discord_user
            ? 'as ' + view.status.discord_user
            : view.status.message) || '';
        setText('drpc-status-detail', detail);

        var toggle = $('drpc-enabled');
        if (toggle) {
            toggle.setAttribute('aria-checked', cfg.enabled ? 'true' : 'false');
            toggle.dataset.on = cfg.enabled ? '1' : '0';
        }
        OPTION_KEYS.forEach(function (key) {
            var box = $('drpc-' + key.replace('_', '-'));
            if (box) box.checked = !!cfg[key];
        });
        var styleSelect = $('drpc-timer-style');
        if (styleSelect && document.activeElement !== styleSelect) styleSelect.value = cfg.timer_style;
        var idInput = $('drpc-client-id');
        if (idInput && document.activeElement !== idInput) idInput.value = cfg.client_id || '';
        setText('drpc-app-source', cfg.client_id
            ? 'Using your own Discord app. Clear the field and Save to switch back to the official one.'
            : 'Using the official Fee[dB]ack Discord app — nothing to set up.');

        var act = view.activity;
        setText('drpc-preview-kicker', act && act.type === 2 ? 'Listening to' : 'Playing a game');
        var card = $('drpc-preview');
        if (card) card.dataset.live = act ? (view.broadcast ? 'live' : 'local') : 'idle';
        setText('drpc-preview-badge', act ? (view.broadcast ? 'Broadcasting' : 'Preview only — Rich Presence is off') : 'Not broadcasting');
        setText('drpc-preview-details', act ? (act.details || '') : 'Nothing to show');
        setText('drpc-preview-state', act ? (act.state || '')
            : (cfg.show_idle ? '' : 'Your presence only shows while a song is playing.'));
        var small = act && act.assets;
        var coverUrl = small && /^https:\/\//.test(small.large_image || '') ? small.large_image : '';
        var coverImg = $('drpc-preview-cover');
        if (coverImg) {
            if (coverImg.getAttribute('src') !== coverUrl) {
                if (coverUrl) coverImg.setAttribute('src', coverUrl); else coverImg.removeAttribute('src');
            }
            coverImg.hidden = !coverUrl;
            coverImg.alt = coverUrl ? ((small && small.large_text) || 'Album cover') : '';
        }
        setText('drpc-preview-icon', (small && small.small_image) || '');
        var icon = $('drpc-preview-icon');
        if (icon) icon.title = (small && small.small_text) || '';
        setText('drpc-preview-instrument', (small && small.small_text) || '');
        renderTimer();
    }

    function refreshStatus() {
        return request('/status').then(function (res) {
            if (!res || !res.ok || destroyed) return;
            view.status = res;
            // While broadcasting, the server's last *sent* activity is the source
            // of truth for the preview (it may lag our last POST by a rate-limit
            // window). A local preview-only activity (presence off) is kept as is.
            if (view.broadcast && playing) {
                view.activity = res.last_activity || view.activity;
            }
            render();
        });
    }

    function parseConfig(res) {
        var out = { enabled: !!res.enabled, client_id: res.client_id || '',
                    timer_style: res.timer_style === 'bar' ? 'bar' : 'elapsed' };
        OPTION_KEYS.forEach(function (key) { out[key] = res[key] !== false; });
        return out;
    }

    function loadConfig() {
        return request('/config').then(function (res) {
            if (!res || !res.ok) return;
            cfg = parseConfig(res);
            render();
        });
    }

    function saveConfig(changes) {
        setText('drpc-config-msg', 'Saving…');
        return request('/config', changes).then(function (res) {
            if (!res || !res.ok) {
                setText('drpc-config-msg', (res && res.error) || 'Could not save settings.');
                render();   // snap controls back to the stored values
                return;
            }
            cfg = parseConfig(res);
            setText('drpc-config-msg', 'Saved.');
            render();
            if (!cfg.enabled) sendClear();
            idleCleared = false;
            sync();
            setTimeout(refreshStatus, 400);
        });
    }

    var panelWired = false;
    function wirePanel() {
        if (panelWired || !$('drpc-enabled')) return;
        panelWired = true;
        listen($('drpc-enabled'), 'click', function () {
            saveConfig({ enabled: !cfg.enabled });
        });
        listen($('drpc-client-form'), 'submit', function (e) {
            e.preventDefault();
            var value = String(($('drpc-client-id') || {}).value || '').trim();
            if (value && !/^\d{15,25}$/.test(value)) {
                setText('drpc-config-msg', 'The Application ID is the long number from the Discord Developer Portal.');
                return;
            }
            saveConfig({ client_id: value });
        });
        var styleSelect = $('drpc-timer-style');
        listen(styleSelect, 'change', function () { saveConfig({ timer_style: styleSelect.value }); });
        OPTION_KEYS.forEach(function (key) {
            var box = $('drpc-' + key.replace('_', '-'));
            listen(box, 'change', function () {
                var change = {};
                change[key] = !!box.checked;
                saveConfig(change);
            });
        });
    }

    function onPanelShown() {
        panelVisible = true;
        wirePanel();
        loadConfig();
        refreshStatus();
        if (!timers.poll) timers.poll = setInterval(refreshStatus, STATUS_POLL_MS);
        if (!timers.tick) timers.tick = setInterval(renderTimer, 1000);
    }

    function onPanelHidden() {
        panelVisible = false;
        // Performance rule: no background loops for UI nobody can see.
        if (timers.poll) { clearInterval(timers.poll); timers.poll = null; }
        if (timers.tick) { clearInterval(timers.tick); timers.tick = null; }
    }

    // ── Public API + teardown ───────────────────────────────────────────────
    function destroy() {
        destroyed = true;
        stopTimers();
        onPanelHidden();
        listeners.forEach(function (entry) {
            try {
                if (entry[0] === bus) bus.off(entry[1], entry[2]);
                else entry[0].removeEventListener(entry[1], entry[2]);
            } catch (_) { /* noop */ }
        });
        listeners = [];
    }

    bus.discordRpc = {
        version: 1,
        reportScore: reportScore,
        getState: function () {
            return { playing: playing, song: song, score: Object.assign({}, score),
                     config: Object.assign({}, cfg), activity: view.activity, broadcast: view.broadcast };
        },
        refresh: function () { sync(); },
        destroy: destroy,
    };

    // Boot: pick up config once (so the first preview is right), and if the
    // user is already on our screen (deep link / reload), initialise it.
    var current = document.querySelector('.screen.active');
    currentScreen = current ? current.id : null;
    if (currentScreen === SCREEN_ID) onPanelShown();
    if (bus.isPlaying && bus.currentSong) { song = readSong(); playing = true; }
    loadConfig().then(sync);   // first presence once we know whether it's enabled
})();
