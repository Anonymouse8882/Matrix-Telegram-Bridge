"""Matrix source adapter.

Implements the `MessageSource` and `MediaFetcher` ports on top of matrix-nio.
It logs in (token or password), syncs, and translates each supported room
event into a neutral `InboundMessage`. All the Matrix-specific knowledge
(mxc:// URLs, event classes, sync tokens) is quarantined in this file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadError,
    LoginResponse,
    MatrixRoom,
    RedactionEvent,
    RoomMessageAudio,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    RoomMessageVideo,
    SyncResponse,
)
from nio.events.room_events import Event

try:  # StickerEvent lives in different places across nio versions
    from nio.events.room_events import StickerEvent
except ImportError:  # pragma: no cover
    StickerEvent = None  # type: ignore

from ..core.models import InboundMessage, MediaRef, MessageKind
from ..core.ports import MessageHandler
from ..proxy import aiohttp_session, http_proxy_url, parse_proxy

log = logging.getLogger(__name__)

# Custom content key stamped on every message the bridge itself posts, so we
# can recognise (and skip) our own relayed messages and never loop.
BRIDGE_ORIGIN_KEY = "space.bridge.origin"

# Members are lazy-loaded on every sync: the bridge only reads the owner's own
# events, so downloading full member lists for every room is pure overhead.
_SYNC_FILTER = {"room": {"state": {"lazy_load_members": True}}}
# On a cold start (no saved sync token) the backlog is discarded by timestamp
# anyway, so don't download it: one timeline event per room is enough to get a
# next_batch and the room list.
_FIRST_SYNC_FILTER = {
    "room": {"timeline": {"limit": 1}, "state": {"lazy_load_members": True}}
}


class MatrixSource:
    def __init__(
        self,
        homeserver: str,
        user_id: str,
        access_token: str = "",
        password: str = "",
        device_id: str = "MATRIX_TG_BRIDGE",
        store_path: str = "./store",
        watched_rooms: Optional[set[str]] = None,
        proxy_url: str = "",
        command_prefix: Optional[Callable[[], str]] = None,
    ):
        self._user_id = user_id
        self._password = password
        self._access_token = access_token
        self._device_id = device_id
        self._watched = watched_rooms or set()
        # Read lazily: the prefix is configurable at runtime, and account
        # commands must work in rooms that are not the bridge's own.
        self._command_prefix = command_prefix
        self._handler: Optional[MessageHandler] = None
        self._redaction_handler = None  # Callable[[str], Awaitable[None]]
        self._started_ms = 0
        # Handlers run in per-room worker queues, NOT inline: nio awaits event
        # callbacks inside the sync loop, so a slow command or media transfer
        # would otherwise stall every other room until it finished.
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: list[asyncio.Task] = []
        # The last sync token, persisted so a restart resumes instead of doing
        # a full initial sync (which grows with every per-chat room created).
        self._token_path = os.path.join(store_path, "sync_token.txt")
        self._saved_token = ""

        os.makedirs(store_path, exist_ok=True)  # nio requires the dir to exist
        config = AsyncClientConfig(store_sync_tokens=True)
        proxy = parse_proxy(proxy_url)
        self._client = AsyncClient(
            homeserver,
            user_id,
            device_id=device_id,
            store_path=store_path,
            config=config,
            proxy=http_proxy_url(proxy),  # None unless it's an HTTP proxy
        )
        # SOCKS needs a custom connector rather than nio's `proxy=`; handing nio
        # a ready-made session covers sync, sends and media downloads alike.
        self._proxy_session = aiohttp_session(proxy)
        if self._proxy_session is not None:
            self._client.client_session = self._proxy_session
        if proxy is not None:
            log.info("matrix traffic via proxy %s", proxy.sanitised())

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def watch(self, room_id: str) -> None:
        """Add a room to the watched set (per-chat rooms created at runtime)."""
        self._watched.add(room_id)

    def set_redaction_handler(self, handler) -> None:
        """handler(redacted_event_id: str) is called when the owner deletes a
        message in the control room."""
        self._redaction_handler = handler

    @property
    def client(self) -> AsyncClient:
        """The underlying nio client, shared with the Matrix sink for sending."""
        return self._client

    async def start(self) -> None:
        await self._login()
        self._register_callbacks()
        # Ignore backlog: only forward events that arrive after we're live.
        self._started_ms = int(time.time() * 1000)
        since = self._load_sync_token()
        self._client.add_response_callback(self._on_sync_response, SyncResponse)
        log.info("matrix sync starting as %s%s", self._user_id,
                 " (resuming)" if since else " (initial sync)")
        await self._client.sync_forever(
            timeout=30_000,
            since=since or None,
            sync_filter=_SYNC_FILTER,
            # With a token the first sync is just a resume; without one, keep
            # the initial sync as small as possible — its events are dropped
            # by the timestamp guard anyway.
            first_sync_filter=None if since else _FIRST_SYNC_FILTER,
        )

    async def _login(self) -> None:
        if self._access_token:
            # restore_login (not bare attribute writes) also loads nio's store
            # where available, so device state survives restarts.
            try:
                self._client.restore_login(
                    self._user_id, self._device_id or "", self._access_token
                )
            except Exception:  # noqa: BLE001 - a bad store must not stop login
                log.exception("could not restore the nio store; continuing")
                self._client.user_id = self._user_id
                self._client.access_token = self._access_token
                if self._device_id:
                    self._client.device_id = self._device_id
            return
        resp = await self._client.login(
            self._password, device_name=self._device_id
        )
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"matrix login failed: {resp}")
        log.info("matrix login ok (device %s)", resp.device_id)

    # -- sync-token persistence ----------------------------------------------

    def _load_sync_token(self) -> str:
        try:
            with open(self._token_path, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            return ""
        self._saved_token = token
        return token

    async def _on_sync_response(self, response: SyncResponse) -> None:
        token = getattr(response, "next_batch", "") or ""
        if not token or token == self._saved_token:
            return
        try:
            tmp = f"{self._token_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(token)
            os.replace(tmp, self._token_path)
            self._saved_token = token
        except OSError:
            log.warning("could not persist the matrix sync token")

    def _register_callbacks(self) -> None:
        self._client.add_event_callback(self._on_text, RoomMessageText)
        self._client.add_event_callback(self._on_image, RoomMessageImage)
        self._client.add_event_callback(self._on_video, RoomMessageVideo)
        self._client.add_event_callback(self._on_audio, RoomMessageAudio)
        self._client.add_event_callback(self._on_file, RoomMessageFile)
        self._client.add_event_callback(self._on_redaction, RedactionEvent)
        if StickerEvent is not None:
            self._client.add_event_callback(self._on_sticker, StickerEvent)

    # -- event guards --------------------------------------------------------

    def _should_process(self, room: MatrixRoom, event: Event) -> bool:
        content = getattr(event, "source", {}).get("content", {})
        if BRIDGE_ORIGIN_KEY in content:
            return False  # a message the bridge itself relayed — never loop
        if event.sender != self._user_id:
            return False  # only the owner account drives the bridge
        ts = getattr(event, "server_timestamp", 0) or 0
        if ts and ts < self._started_ms:
            return False  # historical backlog
        if self._watched and room.room_id not in self._watched:
            # Outside the bridge's own rooms only *commands* are of interest —
            # that is what lets an account be logged in and bound from inside
            # the Space it belongs to. Plain chat elsewhere is left alone.
            return self._is_command(content)
        return True

    def _is_command(self, content: dict) -> bool:
        if self._command_prefix is None:
            return False
        prefix = (self._command_prefix() or "").strip()
        body = content.get("body")
        return bool(prefix) and isinstance(body, str) and body.startswith(prefix)

    def _sender_name(self, room: MatrixRoom, event: Event) -> str:
        return room.user_name(event.sender) or event.sender

    async def _emit(self, msg: InboundMessage) -> None:
        if self._handler is not None:
            self._enqueue(msg.source_room, lambda: self._handler(msg))

    def _enqueue(self, room_id: str, work: Callable[[], Awaitable[None]]) -> None:
        """Queue work per room instead of running it inside the sync loop.

        nio awaits event callbacks inline, so a slow handler (a media transfer,
        a `stats` command) would otherwise block every further sync round. One
        queue per room keeps messages within a room strictly ordered while a
        busy room cannot delay the others.
        """
        queue = self._queues.get(room_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[room_id] = queue
            self._workers.append(asyncio.create_task(self._drain(queue)))
        queue.put_nowait(work)

    async def _drain(self, queue: asyncio.Queue) -> None:
        while True:
            work = await queue.get()
            try:
                await work()
            except Exception:  # noqa: BLE001 - one bad event mustn't kill the room
                log.exception("matrix event handler failed")
            finally:
                queue.task_done()

    # -- per-kind callbacks --------------------------------------------------

    async def _on_text(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if not self._should_process(room, event):
            return
        content = getattr(event, "source", {}).get("content", {})
        reply_event = (
            content.get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
        )
        body = _strip_reply_fallback(event.body) if reply_event else event.body
        await self._emit(
            InboundMessage(
                kind=MessageKind.TEXT,
                source_room=room.room_id,
                sender=self._sender_name(room, event),
                text=body,
                reply_to_event=reply_event,
                event_id=event.event_id,
            )
        )

    async def _on_media(
        self, room: MatrixRoom, event: Event, kind: MessageKind
    ) -> None:
        if not self._should_process(room, event):
            return
        info = getattr(event, "source", {}).get("content", {}).get("info", {})
        media = MediaRef(
            uri=getattr(event, "url", "") or "",
            mimetype=info.get("mimetype"),
            filename=getattr(event, "body", None),
            size=info.get("size"),
        )
        if not media.uri:
            log.warning("media event without url in %s", room.room_id)
            return
        await self._emit(
            InboundMessage(
                kind=kind,
                source_room=room.room_id,
                sender=self._sender_name(room, event),
                text=None,  # body is the filename for media; not a caption
                media=media,
                event_id=getattr(event, "event_id", None),
            )
        )

    async def _on_image(self, room: MatrixRoom, event: RoomMessageImage) -> None:
        await self._on_media(room, event, MessageKind.IMAGE)

    async def _on_video(self, room: MatrixRoom, event: RoomMessageVideo) -> None:
        await self._on_media(room, event, MessageKind.VIDEO)

    async def _on_audio(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        await self._on_media(room, event, MessageKind.AUDIO)

    async def _on_file(self, room: MatrixRoom, event: RoomMessageFile) -> None:
        await self._on_media(room, event, MessageKind.FILE)

    async def _on_sticker(self, room: MatrixRoom, event: Event) -> None:
        await self._on_media(room, event, MessageKind.STICKER)

    async def _on_redaction(self, room: MatrixRoom, event: RedactionEvent) -> None:
        if self._watched and room.room_id not in self._watched:
            return
        if event.sender != self._user_id:
            return  # only the owner's deletions drive TG deletion
        ts = getattr(event, "server_timestamp", 0) or 0
        if ts and ts < self._started_ms:
            return
        redacts = getattr(event, "redacts", None)
        if redacts and self._redaction_handler is not None:
            # Same per-room queue as messages, so a redaction cannot overtake
            # the message it follows.
            handler = self._redaction_handler
            self._enqueue(room.room_id, lambda: handler(redacts))

    # -- MediaFetcher port ---------------------------------------------------

    async def fetch(self, ref: MediaRef) -> bytes:
        server_name, media_id = _parse_mxc(ref.uri)
        try:  # newer nio: download(mxc=...)
            resp = await self._client.download(mxc=ref.uri)
        except TypeError:  # older nio: download(server_name, media_id)
            resp = await self._client.download(server_name, media_id)
        if isinstance(resp, DownloadError):
            raise RuntimeError(f"matrix download failed: {resp}")
        return resp.body

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()
        self._queues.clear()
        await self._client.close()
        # nio only closes sessions it created itself, so ours needs closing too.
        if self._proxy_session is not None and not self._proxy_session.closed:
            await self._proxy_session.close()


def _strip_reply_fallback(body: str) -> str:
    """Element prefixes a reply's plain body with quoted `> ` lines then a
    blank line. Drop them so only the actual reply text goes to Telegram."""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:]).strip() or body


def _parse_mxc(uri: str) -> tuple[str, str]:
    if not uri.startswith("mxc://"):
        raise ValueError(f"not an mxc URI: {uri}")
    server_and_id = uri[len("mxc://"):]
    server_name, _, media_id = server_and_id.partition("/")
    return server_name, media_id
