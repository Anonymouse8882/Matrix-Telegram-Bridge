from bridge.core.transformer import (
    UNKNOWN_ORIGIN,
    escape_html,
    format_incoming,
    html_to_plain,
    with_forward,
)


def test_escape_html():
    assert escape_html("a<b>&c") == "a&lt;b&gt;&amp;c"


def test_html_to_plain_roundtrips_entities():
    assert html_to_plain("<b>Al</b>: a&lt;b&amp;c") == "Al: a<b&c"


def test_format_incoming_with_body():
    assert format_incoming("群名", "张三", "你好") == \
        "<b>[群名]</b> <b>张三</b>: 你好"


def test_format_incoming_without_body():
    assert format_incoming("群名", "张三", "") == "<b>[群名]</b> <b>张三</b>"


def test_format_incoming_escapes_all_parts():
    out = format_incoming("a<b", "c>d", "e&f")
    assert out == "<b>[a&lt;b]</b> <b>c&gt;d</b>: e&amp;f"


def test_with_forward_leaves_a_normal_message_alone():
    head = format_incoming("群名", "张三", "")
    assert with_forward(head, None) == head


def test_with_forward_appends_the_marker():
    assert with_forward("<b>张三</b>", "李四") == \
        "<b>张三</b> <i>↪️ 转发自 李四</i>"


def test_with_forward_is_the_whole_head_in_a_dm_room():
    """A DM's own room renders no sender, so the marker stands alone."""
    assert with_forward("", "李四") == "<i>↪️ 转发自 李四</i>"


def test_with_forward_escapes_the_origin_name():
    assert "a&lt;b" in with_forward("", "a<b")


def test_with_forward_names_a_hidden_origin():
    assert UNKNOWN_ORIGIN in with_forward("", "")
