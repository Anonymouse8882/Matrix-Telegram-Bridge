"""Matrix sink adapter: posts messages into a Matrix room.

Implements `MessageSink` on top of the SAME nio client the source uses (shared
from MatrixSource.client). Every message it sends carries the BRIDGE_ORIGIN_KEY
flag so the source skips it and no loop forms. Muted messages go as `m.notice`,
which clients treat as low-priority / non-notifying.
"""

from __future__ import annotations

import io
import logging
from dataclasses import replace

from nio import AsyncClient

from ..core.models import MessageKind, OutboundMessage, Target
from ..core.ports import MessageSink
from ..core.transformer import html_to_plain
from .matrix_source import BRIDGE_ORIGIN_KEY

log = logging.getLogger(__name__)

# Map our media kinds onto Matrix msgtypes.
_MSGTYPE = {
    MessageKind.IMAGE: "m.image",
    MessageKind.STICKER: "m.image",
    MessageKind.VIDEO: "m.video",
    MessageKind.AUDIO: "m.audio",
    MessageKind.FILE: "m.file",
}


def _add_reply(content: dict, event_id: str | None) -> None:
    """Attach an m.in_reply_to relation, mirroring a Telegram reply.

    Element then draws the same quoted-above-the-message threading Telegram
    shows, so a conversation between other people stays readable here.
    """
    if not event_id:
        return
    content["m.relates_to"] = {"m.in_reply_to": {"event_id": event_id}}


class MatrixSink(MessageSink):
    def __init__(self, client: AsyncClient):
        self._client = client

    async def deliver(
        self, target: Target, message: OutboundMessage
    ) -> str | None:
        room_id = target.chat_id  # a Matrix room id, for this sink
        if message.kind is MessageKind.TEXT or not message.media_bytes:
            return await self._send_text(room_id, message)
        return await self._send_media(room_id, message)

    async def _send_text(
        self, room_id: str, message: OutboundMessage
    ) -> str | None:
        html = message.text or ""
        content = {
            # m.notice = don't notify (muted); m.text = normal.
            "msgtype": "m.notice" if message.silent else "m.text",
            "body": html_to_plain(html) if message.html else html,
            BRIDGE_ORIGIN_KEY: "telegram",
        }
        if message.html:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html
        _add_reply(content, message.reply_to_event)
        resp = await self._client.room_send(room_id, "m.room.message", content)
        return getattr(resp, "event_id", None)

    async def _send_media(
        self, room_id: str, message: OutboundMessage
    ) -> str | None:
        data = message.media_bytes or b""
        filename = message.filename or "file"
        mimetype = message.mimetype or "application/octet-stream"

        resp, _ = await self._client.upload(
            lambda *_: io.BytesIO(data),
            content_type=mimetype,
            filename=filename,
            filesize=len(data),
        )
        content_uri = getattr(resp, "content_uri", None)
        if not content_uri:
            log.error("matrix upload failed: %s", resp)
            # Fall back to just the caption so the message isn't lost entirely.
            return await self._send_text(room_id, message)

        content = {
            "msgtype": _MSGTYPE.get(message.kind, "m.file"),
            "body": message.text and html_to_plain(message.text) or filename,
            "url": content_uri,
            "info": {"mimetype": mimetype, "size": len(data)},
            BRIDGE_ORIGIN_KEY: "telegram",
        }
        _add_reply(content, message.reply_to_event)
        media_resp = await self._client.room_send(
            room_id, "m.room.message", content
        )
        # Media has no caption field in Matrix; send the label line alongside.
        # Without the reply relation: the media event above already carries it.
        if message.text:
            await self._send_text(room_id, replace(message, reply_to_event=None))
        return getattr(media_resp, "event_id", None)

    async def redact(self, room_id: str, event_id: str) -> None:
        """Delete a single Matrix event (used by self-destruct sync)."""
        await self._client.room_redact(room_id, event_id, reason="self-destruct")

    async def replace_event(self, room_id: str, event_id: str, html: str) -> None:
        """Edit an existing event in place via m.replace.

        Used to *mark* a remotely-deleted Telegram message rather than redact
        it: the text stays readable, struck through, so history is preserved.
        """
        new_content = {
            "msgtype": "m.text",
            "body": html_to_plain(html),
            "format": "org.matrix.custom.html",
            "formatted_body": html,
        }
        content = {
            **new_content,
            # Fallback body for clients that don't understand edits.
            "body": " * " + html_to_plain(html),
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
            BRIDGE_ORIGIN_KEY: "telegram",
        }
        await self._client.room_send(room_id, "m.room.message", content)

    async def annotate_event(self, room_id: str, event_id: str, html: str) -> None:
        """Post a non-notifying note as a reply to an event (media markers,
        edit diffs) — anchors the note to the message it is about."""
        content = {
            "msgtype": "m.notice",
            "body": html_to_plain(html),
            "format": "org.matrix.custom.html",
            "formatted_body": html,
            "m.relates_to": {"m.in_reply_to": {"event_id": event_id}},
            BRIDGE_ORIGIN_KEY: "telegram",
        }
        await self._client.room_send(room_id, "m.room.message", content)

    async def close(self) -> None:
        # The client is owned by MatrixSource, which closes it.
        pass
