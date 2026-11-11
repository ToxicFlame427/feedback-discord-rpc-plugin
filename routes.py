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
"""Discord Rich Presence plugin — FastAPI routes.

SETUP
-----
* Python packages: none beyond Fee[dB]ack's own (FastAPI + pydantic). The
  Discord IPC bridge in ``discord_ipc.py`` is pure standard library, so there
  is deliberately no ``requirements.txt`` and no ``pip install pypresence``.
* Discord side: nothing. The plugin ships with the official Fee[dB]ack
  Discord application (``presence.DEFAULT_CLIENT_ID``); users only flip the
  Broadcast switch. A custom Application ID can be set in the panel to use
  your own app (forks / own branding) — it needs Rich Presence art assets
  keyed ``app_logo``, ``guitar_icon``, ``bass_icon``, ``drums_icon``,
  ``keys_icon``, ``vocals_icon``.
* The Fee[dB]ack backend must run on the same machine (same user session) as
  the Discord desktop app — the bridge talks to a local socket / named pipe.
  Docker/NAS installs need the socket mounted in; see README.md.

ENDPOINTS (all under /api/plugins/discord_rpc)
----------------------------------------------
  POST /presence state payload from screen.js → queued Discord activity
                 (NOT /update: core owns POST /api/plugins/{id}/update — the
                 plugin self-updater, registered before plugins load, so a
                 plugin route at that path is unreachable)
  POST /clear    clear the activity (pause / stop / menus)
  GET  /status   connection state + the exact activity last broadcast
  GET  /config   plugin settings
  POST /config   update settings (enabled, client_id, show_score, show_tuning, show_timestamps, show_idle,
                 timer_style, show_cover)

Every handler is an error boundary: Discord being closed, a bad payload, or
a bug in this plugin yields a JSON ``{"ok": false, ...}`` and a debug log line
— never an exception that propagates into Fee[dB]ack.
"""

import atexit
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

PLUGIN_ID = "discord_rpc"
API_PREFIX = "/api/plugins/" + PLUGIN_ID
CONFIG_FILENAME = "discord_rpc.json"  # listed in plugin.json settings.server_files


class PresenceUpdate(BaseModel):
    """State payload posted by screen.js. Unknown keys are ignored."""

    mode: Optional[str] = Field(None, max_length=16)            # playing | paused | browsing
    activityLabel: Optional[str] = Field(None, max_length=128)  # browsing: "Browsing the song library"
    sessionStart: Optional[float] = Field(None, ge=0, le=1e11)  # browsing: when Fee[dB]ack was opened (epoch s)
    filename: Optional[str] = Field(None, max_length=2048)      # library key → album cover lookup
    songTitle: str = Field("", max_length=512)
    artist: str = Field("", max_length=512)
    instrument: str = Field("", max_length=128)
    instrumentKey: Optional[str] = Field(None, max_length=64)
    tuning: Optional[str] = Field(None, max_length=64)          # e.g. "E Standard", "Drop D"
    score: Optional[float] = Field(None, ge=0, le=1e12)
    accuracy: Optional[float] = Field(None, ge=0, le=100)          # percent
    duration: Optional[float] = Field(None, ge=0, le=86400)        # seconds
    currentTime: Optional[float] = Field(None, ge=0, le=86400)     # seconds
    playbackRate: Optional[float] = Field(None, gt=0, le=4)


class ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    client_id: Optional[str] = Field(None, max_length=32)
    show_score: Optional[bool] = None
    show_tuning: Optional[bool] = None
    show_idle: Optional[bool] = None
    timer_style: Optional[str] = Field(None, max_length=16)     # "elapsed" | "bar"
    show_cover: Optional[bool] = None
    show_timestamps: Optional[bool] = None


def _dump(model):
    """pydantic v1/v2 compatible ``exclude_none`` dump."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def setup(app: FastAPI, context: dict):
    log = context.get("log") or logging.getLogger("feedBack.plugin." + PLUGIN_ID)
    load_sibling = context.get("load_sibling")
    if load_sibling is not None:
        ipc_mod = load_sibling("discord_ipc")
        presence = load_sibling("presence")
    else:  # very old hosts without load_sibling (pre feedBack#33)
        import discord_ipc as ipc_mod  # type: ignore
        import presence  # type: ignore

    store = presence.ConfigStore(Path(context["config_dir"]) / CONFIG_FILENAME)
    covers = presence.CoverResolver(context.get("meta_db"), log=log)

    # Idempotent re-setup (plugin disable → enable, hot reload): retire the
    # previous worker so there is only ever one IPC connection per process.
    previous = getattr(app.state, "discord_rpc_service", None)
    if previous is not None:
        try:
            previous.stop()
        except Exception:  # noqa: BLE001
            pass

    service = presence.PresenceService(
        store,
        ipc_factory=lambda client_id: ipc_mod.DiscordIPC(client_id),
        rejected_errors=(ipc_mod.DiscordRPCError,),
        log=log,
    )
    service.start()
    app.state.discord_rpc_service = service
    if not getattr(app.state, "discord_rpc_atexit", False):
        # Clear the presence on a clean shutdown. (On a hard kill Discord
        # clears it anyway once our pid's socket closes.)
        atexit.register(lambda: getattr(app.state, "discord_rpc_service", None) and app.state.discord_rpc_service.stop(1.0))
        app.state.discord_rpc_atexit = True

    if getattr(app.state, "discord_rpc_routes_mounted", False):
        log.info("discord_rpc: re-initialised (routes already mounted)")
        return

    def _svc():
        # Resolved per request so a re-setup swaps the worker under live routes.
        return app.state.discord_rpc_service

    def _fail(message, exc, status_code=200):
        log.debug("discord_rpc: %s: %r", message, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)

    router = APIRouter(prefix=API_PREFIX, tags=["discord_rpc"])

    @router.post("/presence")
    def update_presence(payload: PresenceUpdate):
        try:
            svc = _svc()
            cfg = svc.store.read()
            cover = None
            if cfg["show_cover"] and payload.filename and (payload.mode or "playing") != "browsing":
                cover = covers.lookup(payload.filename)
            activity = presence.build_activity(
                _dump(payload),
                show_score=cfg["show_score"],
                show_tuning=cfg["show_tuning"],
                timer_style=cfg["timer_style"],
                cover=cover,
                show_timestamps=cfg["show_timestamps"],
            )
            broadcast = bool(cfg["enabled"] and presence.effective_client_id(cfg))
            idle = (payload.mode or "playing") != "playing"
            if idle and not cfg["show_idle"]:
                activity, broadcast = None, False     # user only wants presence while playing
                svc.clear()
            elif broadcast:
                svc.submit(activity)
            return {"ok": True, "broadcast": broadcast, "activity": activity,
                    "status": svc.status()["state"]}
        except Exception as exc:  # noqa: BLE001
            return _fail("update failed", exc)

    @router.post("/clear")
    def clear_presence():
        try:
            _svc().clear()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return _fail("clear failed", exc)

    @router.get("/status")
    def get_status():
        try:
            return {"ok": True, **_svc().status()}
        except Exception as exc:  # noqa: BLE001
            return _fail("status unavailable", exc)

    @router.get("/config")
    def get_config():
        try:
            return {"ok": True, **_svc().store.read()}
        except Exception as exc:  # noqa: BLE001
            return _fail("config unavailable", exc)

    @router.post("/config")
    def set_config(body: ConfigUpdate):
        try:
            cfg = _svc().store.update(_dump(body))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return _fail("could not save config", exc)
        _svc().reconfigure()
        return {"ok": True, **cfg}

    app.include_router(router)
    app.state.discord_rpc_routes_mounted = True
    log.info("discord_rpc: routes mounted at %s", API_PREFIX)
