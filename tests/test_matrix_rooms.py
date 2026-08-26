"""MatrixRooms avatar mirroring, with a fake nio client (no homeserver)."""

from bridge.adapters.matrix_rooms import MatrixRooms

ROOM = "!room:matrix.org"


class FakeUploadResp:
    def __init__(self, uri="mxc://server/avatar1"):
        self.content_uri = uri


class FakeMxClient:
    def __init__(self, upload_uri="mxc://server/avatar1"):
        self.uploads: list[tuple] = []
        self.state: list[tuple] = []  # (room_id, type, content, state_key)
        self._uri = upload_uri

    async def upload(self, provider, content_type, filename, filesize):
        self.uploads.append((content_type, filename, filesize))
        return FakeUploadResp(self._uri), None

    async def room_put_state(self, room_id, kind, content, state_key=""):
        self.state.append((room_id, kind, content, state_key))
        return type("R", (), {"event_id": "$s"})()


class FakeDir:
    """A directory handing out avatar bytes; counts how often it is asked."""

    def __init__(self, photo=b"JPEGBYTES"):
        self.photo = photo
        self.calls: list[int] = []

    async def avatar(self, chat_id):
        self.calls.append(chat_id)
        return self.photo


def _rooms(client, directory):
    return MatrixRooms(client, space_id="", homeserver_name="hs",
                       directory=directory)


async def test_avatar_is_uploaded_and_set_as_room_state():
    client, d = FakeMxClient(), FakeDir()
    rooms = _rooms(client, d)

    assert await rooms.set_avatar(ROOM, 111) == "set"

    assert client.uploads == [("image/jpeg", "avatar.jpg", len(b"JPEGBYTES"))]
    room_id, kind, content, _key = client.state[0]
    assert room_id == ROOM
    assert kind == "m.room.avatar"
    assert content == {"url": "mxc://server/avatar1"}


async def test_an_unchanged_photo_is_not_re_uploaded():
    """`!tg info` and `avatar all` re-run this constantly; without the guard
    every run would upload again and post an avatar-change event."""
    client, d = FakeMxClient(), FakeDir()
    rooms = _rooms(client, d)

    assert await rooms.set_avatar(ROOM, 111) == "set"
    assert await rooms.set_avatar(ROOM, 111) == "unchanged"

    assert len(client.uploads) == 1
    assert len(client.state) == 1
    assert d.calls == [111, 111]  # still asked Telegram; just did not re-upload


async def test_a_changed_photo_is_mirrored_again():
    client, d = FakeMxClient(), FakeDir()
    rooms = _rooms(client, d)
    await rooms.set_avatar(ROOM, 111)

    d.photo = b"DIFFERENT"
    assert await rooms.set_avatar(ROOM, 111) == "set"
    assert len(client.uploads) == 2


async def test_a_chat_without_a_photo_is_a_no_op():
    client, d = FakeMxClient(), FakeDir(photo=None)
    rooms = _rooms(client, d)

    assert await rooms.set_avatar(ROOM, 111) == "none"
    assert client.uploads == [] and client.state == []


async def test_two_chats_do_not_share_the_avatar_cache():
    client, d = FakeMxClient(), FakeDir()
    rooms = _rooms(client, d)

    assert await rooms.set_avatar("!a:hs", 111) == "set"
    assert await rooms.set_avatar("!b:hs", 222) == "set"  # same bytes, other chat
    assert len(client.uploads) == 2


async def test_a_failed_upload_does_not_poison_the_cache():
    """A transient upload failure must not make the next attempt think the
    avatar is already in place."""
    client = FakeMxClient(upload_uri="")  # no content_uri == failure
    rooms = _rooms(client, FakeDir())

    assert await rooms.set_avatar(ROOM, 111) == "error"
    assert client.state == []

    client._uri = "mxc://server/avatar1"
    assert await rooms.set_avatar(ROOM, 111) == "set"


async def test_no_directory_means_no_avatar_support():
    client = FakeMxClient()
    rooms = MatrixRooms(client, space_id="", homeserver_name="hs", directory=None)
    assert await rooms.set_avatar(ROOM, 111) == "error"


# -- space lookup ------------------------------------------------------------


class _SpaceServer:
    """A homeserver with `rooms` joined, of which `spaces` are Spaces."""

    def __init__(self, rooms, spaces, child_of=None):
        self._rooms, self._spaces = rooms, set(spaces)
        self._child_of = child_of or {}
        self.calls = 0
        self.peak_in_flight = 0
        self._in_flight = 0

    async def joined_rooms(self):
        from types import SimpleNamespace
        return SimpleNamespace(rooms=list(self._rooms))

    async def room_get_state_event(self, room_id, kind, key):
        from types import SimpleNamespace
        self.calls += 1
        self._in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            import asyncio
            await asyncio.sleep(0)   # let siblings interleave
            if kind == "m.room.create":
                content = {"type": "m.space"} if room_id in self._spaces else {}
            elif kind == "m.space.child" and self._child_of.get(key) == room_id:
                content = {"via": ["hs"]}
            else:
                content = {}
            return SimpleNamespace(content=content)
        finally:
            self._in_flight -= 1

    async def room_get_state(self, room_id):
        from types import SimpleNamespace
        return SimpleNamespace(events=[])


async def test_space_for_room_finds_the_claiming_space():
    from bridge.adapters.matrix_spaces import MatrixSpaces

    server = _SpaceServer(
        rooms=[f"!r{i}:hs" for i in range(20)] + ["!space:hs"],
        spaces=["!space:hs"],
        child_of={"!r7:hs": "!space:hs"},
    )
    assert await MatrixSpaces(server).space_for_room("!r7:hs") == "!space:hs"


async def test_space_scan_probes_rooms_concurrently():
    """One room at a time made `bind` sit through hundreds of round-trips."""
    from bridge.adapters.matrix_spaces import MatrixSpaces

    server = _SpaceServer(rooms=[f"!r{i}:hs" for i in range(40)], spaces=[])
    assert await MatrixSpaces(server).space_for_room("!target:hs") is None
    assert server.peak_in_flight > 1


async def test_space_scan_returns_none_when_no_space_claims_the_room():
    from bridge.adapters.matrix_spaces import MatrixSpaces

    server = _SpaceServer(rooms=["!a:hs", "!space:hs"], spaces=["!space:hs"])
    assert await MatrixSpaces(server).space_for_room("!a:hs") is None
