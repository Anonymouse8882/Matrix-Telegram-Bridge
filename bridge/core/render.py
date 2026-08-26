"""Reply rendering: wrap bridge replies in a distinct monospace panel.

Bridge output goes into the same room the user types in, so it must be visually
separable from their own messages. We render every reply as an HTML <pre> block
framed with box-drawing bars — Element shows it as a monospace card, unlike a
normal chat line. Left-bar-only framing avoids CJK double-width misalignment.
"""

from __future__ import annotations

from .transformer import escape_html

_TOP = "┏━━ "
_BAR = "┃"
_BOT = "┗━━━━━━━━━━━━━━"


def panel(title: str, lines: list[str]) -> str:
    """A titled panel with body lines (plain text; escaped here)."""
    out = [_TOP + escape_html(title) + " ━━"]
    for ln in lines:
        out.append(f"{_BAR} {escape_html(ln)}" if ln else _BAR)
    out.append(_BOT)
    return "<pre>" + "\n".join(out) + "</pre>"


def note(text: str) -> str:
    """A short reply (one or more lines), same distinct style, no title bar."""
    lines = text.split("\n")
    body = "\n".join(f"{_BAR} {escape_html(ln)}" for ln in lines)
    return "<pre>" + body + "</pre>"
