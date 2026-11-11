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
"""Minimal, dependency-free Discord local IPC client.

Setup: NOTHING TO INSTALL. This module uses only the Python standard library,
so the plugin needs no ``requirements.txt`` and no ``pip install pypresence``.
(pypresence speaks the same protocol; we implement the ~150 lines we need
directly so the plugin works in the desktop bundle's frozen Python and on
read-only installs where per-plugin pip installs can fail.)

Wire protocol (identical on every OS)::

    <opcode: uint32 LE> <length: uint32 LE> <length bytes of UTF-8 JSON>

    op 0 HANDSHAKE  {"v": 1, "client_id": "<application id>"}
    op 1 FRAME      {"cmd": "SET_ACTIVITY", "args": {...}, "nonce": "<uuid>"}
    op 2 CLOSE      server is closing; payload carries {code, message}
    op 3 PING / op 4 PONG

Transports:
  * Linux / macOS — a Unix domain socket ``discord-ipc-<0..9>`` in
    ``$XDG_RUNTIME_DIR``, ``$TMPDIR``, ``$TMP``, ``$TEMP`` or ``/tmp``
    (plus the Flatpak / Snap / Vesktop sub-directories those clients use).
  * Windows — the named pipe ``\\\\.\\pipe\\discord-ipc-<0..9>``.

Everything here is blocking by design. It is driven from a single dedicated
worker thread (see ``presence.py``) so it never blocks FastAPI's event loop.
"""

import json
import os
import socket
import struct
import sys
import uuid

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

_HEADER = struct.Struct("<II")
_MAX_FRAME_BYTES = 1 << 20  # Discord replies are a few KB; anything larger is corrupt.
_PIPE_COUNT = 10

# Sub-directories (relative to each temp/runtime base) where sandboxed Discord
# builds put their socket. Order matters only for speed — first hit wins.
_UNIX_SUBDIRS = (
    "",
    "app/com.discordapp.Discord",
    "app/com.discordapp.DiscordCanary",
    "app/com.discordapp.DiscordPTB",
    "app/dev.vencord.Vesktop",
    ".flatpak/dev.vencord.Vesktop/xdg-run",
    "snap.discord",
    "snap.discord-canary",
)


class DiscordIPCError(Exception):
    """Base class for every failure this module raises."""


class DiscordNotFound(DiscordIPCError):
    """No Discord IPC endpoint could be opened (Discord closed / not installed)."""


class DiscordDisconnected(DiscordIPCError):
    """The connection dropped mid-conversation (Discord quit or restarted)."""


class DiscordRPCError(DiscordIPCError):
    """Discord answered but rejected the request (e.g. invalid client id)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def candidate_paths(platform=None, environ=None):
    """Return every IPC endpoint path worth trying, in priority order.

    Unix paths are filtered to those that exist so a closed Discord costs a
    handful of ``stat`` calls rather than ten failed ``connect`` calls each.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform.startswith("win"):
        return [r"\\.\pipe\discord-ipc-%d" % i for i in range(_PIPE_COUNT)]

    bases = []
    for key in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = environ.get(key)
        if value and value not in bases:
            bases.append(value)
    if "/tmp" not in bases:
        bases.append("/tmp")

    paths = []
    for base in bases:
        for sub in _UNIX_SUBDIRS:
            directory = os.path.join(base, sub) if sub else base
            for i in range(_PIPE_COUNT):
                path = os.path.join(directory, "discord-ipc-%d" % i)
                if path not in paths and os.path.exists(path):
                    paths.append(path)
    return paths


class _UnixTransport:
    def __init__(self, path, timeout):
        self._timeout = timeout
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(path)
        except OSError:
            self._sock.close()
            raise

    def write(self, data):
        self._sock.sendall(data)

    def alive(self):
        """Cheap liveness probe: a peer that closed the socket reads as EOF."""
        try:
            self._sock.setblocking(False)
            try:
                return self._sock.recv(1, socket.MSG_PEEK) != b""
            finally:
                self._sock.settimeout(self._timeout)
        except BlockingIOError:
            return True  # nothing to read, connection still open
        except OSError:
            return False

    def read(self, n):
        chunks = []
        remaining = n
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise DiscordDisconnected("Discord closed the IPC socket")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class _PipeTransport:
    """Windows named pipe opened as an unbuffered binary file.

    Named-pipe reads cannot time out without overlapped I/O, so a read only
    happens directly after a write Discord always answers (handshake, frames).
    """

    def __init__(self, path, timeout):  # timeout unused: see class docstring
        self._fh = open(path, "r+b", buffering=0)

    def alive(self):
        return not self._fh.closed  # no non-blocking peek on a plain pipe handle

    def write(self, data):
        self._fh.write(data)

    def read(self, n):
        chunks = []
        remaining = n
        while remaining:
            chunk = self._fh.read(remaining)
            if not chunk:
                raise DiscordDisconnected("Discord closed the IPC pipe")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


class DiscordIPC:
    """One authenticated connection to the local Discord client."""

    def __init__(self, client_id, timeout=3.0, platform=None):
        self.client_id = str(client_id)
        self.timeout = timeout
        self._platform = platform or sys.platform
        self._transport = None
        self.endpoint = None
        self.user = None  # Discord user object from the READY event (username etc.)

    # ── lifecycle ──────────────────────────────────────────────────────────
    @property
    def connected(self):
        return self._transport is not None

    def connect(self):
        """Open the first reachable endpoint and complete the handshake."""
        if self._transport is not None:
            return
        paths = candidate_paths(self._platform)
        if not paths:
            raise DiscordNotFound("No Discord IPC socket found (is the Discord desktop app running?)")
        transport_cls = _PipeTransport if self._platform.startswith("win") else _UnixTransport
        last_error = None
        for path in paths:
            try:
                transport = transport_cls(path, self.timeout)
            except OSError as exc:
                last_error = exc
                continue
            self._transport = transport
            self.endpoint = path
            try:
                self._handshake()
                return
            except DiscordRPCError:
                # Discord is there but refused us (bad client id). Trying the
                # next pipe would just repeat the refusal — surface it now.
                self.close()
                raise
            except (DiscordIPCError, OSError) as exc:
                last_error = exc
                self.close()
        raise DiscordNotFound("Could not open a Discord IPC endpoint: %s" % (last_error,))

    def is_alive(self):
        """True while the endpoint still looks open (best effort, never raises)."""
        if self._transport is None:
            return False
        try:
            ok = self._transport.alive()
        except Exception:  # noqa: BLE001 — a probe must never raise
            ok = False
        if not ok:
            self._transport.close()
            self._transport = None
            self.endpoint = None
        return ok

    def close(self):
        transport, self._transport = self._transport, None
        self.endpoint = None
        if transport is None:
            return
        try:
            transport.write(self._encode(OP_CLOSE, {}))
        except (OSError, DiscordIPCError):
            pass
        transport.close()

    # ── commands ───────────────────────────────────────────────────────────
    def set_activity(self, activity, pid=None):
        """Set (dict) or clear (None) the Rich Presence activity."""
        args = {"pid": pid if pid is not None else os.getpid()}
        if activity is not None:
            args["activity"] = activity
        return self._command("SET_ACTIVITY", args)

    def clear_activity(self, pid=None):
        return self.set_activity(None, pid=pid)

    # ── internals ──────────────────────────────────────────────────────────
    def _handshake(self):
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        op, data = self._recv()
        if op == OP_CLOSE:
            raise DiscordRPCError(
                (data or {}).get("message") or "Discord rejected the handshake",
                code=(data or {}).get("code"),
            )
        if op != OP_FRAME or (data or {}).get("evt") != "READY":
            raise DiscordIPCError("Unexpected handshake reply (op=%s)" % op)
        self.user = ((data.get("data") or {}).get("user")) or None

    def _command(self, cmd, args):
        nonce = str(uuid.uuid4())
        self._send(OP_FRAME, {"cmd": cmd, "args": args, "nonce": nonce})
        # Discord may interleave PINGs or unrelated dispatches; read until the
        # reply carrying our nonce arrives.
        for _ in range(16):
            op, data = self._recv()
            if op == OP_PING:
                self._send(OP_PONG, data)
                continue
            if op == OP_CLOSE:
                self.close()
                raise DiscordDisconnected((data or {}).get("message") or "Discord closed the connection")
            if op != OP_FRAME or (data or {}).get("nonce") != nonce:
                continue
            if data.get("evt") == "ERROR":
                err = data.get("data") or {}
                raise DiscordRPCError(err.get("message") or "Discord returned an error", code=err.get("code"))
            return data
        raise DiscordIPCError("No reply from Discord for %s" % cmd)

    @staticmethod
    def _encode(op, payload):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return _HEADER.pack(op, len(body)) + body

    def _send(self, op, payload):
        if self._transport is None:
            raise DiscordDisconnected("Not connected")
        try:
            self._transport.write(self._encode(op, payload))
        except OSError as exc:
            self.close()
            raise DiscordDisconnected("IPC write failed: %s" % exc) from exc

    def _recv(self):
        if self._transport is None:
            raise DiscordDisconnected("Not connected")
        try:
            op, length = _HEADER.unpack(self._transport.read(_HEADER.size))
            if length > _MAX_FRAME_BYTES:
                raise DiscordIPCError("Oversized IPC frame (%d bytes)" % length)
            raw = self._transport.read(length) if length else b""
        except DiscordIPCError:
            self.close()
            raise
        except (OSError, struct.error) as exc:  # socket.timeout is an OSError
            self.close()
            raise DiscordDisconnected("IPC read failed: %s" % exc) from exc
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError as exc:
            self.close()
            raise DiscordIPCError("Malformed IPC JSON") from exc
        return op, data
