"""Pure text-formatting helpers shared by both directions.

No I/O, no SDKs — just string rendering, so every rule is unit-testable.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import ChatInfo

_KIND_LABEL = {"user": "用户", "bot": "机器人", "group": "群组", "channel": "频道"}
_MEMBER_LABEL = {"group": "成员", "channel": "订阅"}


def escape_html(text: str) -> str:
    """Escape the three characters HTML parse modes reserve."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_plain(html: str) -> str:
    """Best-effort plain-text version of our simple HTML (for Matrix `body`)."""
    text = _TAG_RE.sub("", html)
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


def join_head_body(head: str, body: str) -> str:
    """Combine an already-HTML prefix with plain body text.

    Split out from `format_incoming` so a later edit can re-render a message
    byte-for-byte the way it was first posted, given only the stored head.
    """
    body = escape_html(body or "")
    if not head:
        return body
    return f"{head}: {body}" if body else head


def incoming_head(label: str, sender: str) -> str:
    """The '[chat] sender' prefix for a relayed message."""
    return f"<b>[{escape_html(label)}]</b> <b>{escape_html(sender)}</b>"


# Marks a relayed message as forwarded. A symbol rather than a word so the
# eye catches it at a glance in a busy room, the way Telegram's own arrow does.
FORWARD_MARK = "↪️"

# Telegram allows a forward whose origin is fully hidden (the account forbids
# being linked and left no name). Saying so beats attributing it to nobody.
UNKNOWN_ORIGIN = "未知来源"


def forward_tag(origin: str) -> str:
    """The '↪️ 转发自 X' marker appended to a forwarded message's head."""
    return f"<i>{FORWARD_MARK} 转发自 {escape_html(origin or UNKNOWN_ORIGIN)}</i>"


def with_forward(head: str, origin: Optional[str]) -> str:
    """Add the forward marker to an already-HTML head (no-op if not forwarded).

    Kept separate from `incoming_head` because the marker must survive every
    head shape — including the empty one a DM's own room uses, where the
    forward notice is then the whole prefix.
    """
    if origin is None:
        return head
    tag = forward_tag(origin)
    return f"{head} {tag}" if head else tag


def format_incoming(label: str, sender: str, body: str) -> str:
    """Render a Telegram->Matrix line: '[chat] sender: text' as HTML."""
    return join_head_body(incoming_head(label, sender), body)


def _info_kind_label(info: ChatInfo) -> str:
    # `is_bot` is honoured even when the kind still says "user": links stored
    # before bots were their own kind carry the old value.
    if info.is_bot or info.kind == "bot":
        return "机器人"
    return _KIND_LABEL.get(info.kind, info.kind)


def info_lines(info: ChatInfo) -> list[str]:
    """Plain (non-HTML) lines describing a chat, for `!tg info` panels."""
    lines = [f"类型：{_info_kind_label(info)}" + ("  ✓已验证" if info.verified else "")]
    lines.append(f"名称：{info.title}")
    # Username and numeric id together: the @name is what you type, the number
    # is what survives a rename.
    ident = f"@{info.username} · {info.id}" if info.username else str(info.id)
    lines.append(f"ID：{ident}")
    if info.members is not None:
        lines.append(f"{_MEMBER_LABEL.get(info.kind, '成员')}：{info.members}")
    if info.personal_channel:
        lines.append(f"个人频道：{info.personal_channel}")
    if info.about:
        lines.append("")
        lines.append("简介：" if info.kind != "user" else "简介/Bio：")
        lines.extend(info.about.splitlines() or [info.about])
    return lines


def format_topic(info: ChatInfo) -> str:
    """One-line-ish room topic summarising a chat (plain text for m.room.topic)."""
    bits = [f"{_info_kind_label(info)} · ID {info.id}"]
    if info.username:
        bits.append(f"@{info.username}")
    if info.members is not None:
        bits.append(f"{_MEMBER_LABEL.get(info.kind, '成员')} {info.members}")
    if info.personal_channel:
        bits.append(f"个人频道 {info.personal_channel}")
    head = " · ".join(bits)
    about = " ".join((info.about or "").split())
    if about:
        if len(about) > 300:
            about = about[:300] + "…"
        return f"{head}\n{about}"
    return head
