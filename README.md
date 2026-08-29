# Matrix ⇄ Telegram Bridge (interactive)

*[中文文档](README.zh-CN.md)*

Drive your **real Telegram account** from a single Matrix (Element) room:

- **Send** — type in the room and it goes to your currently selected Telegram
  chat; switch targets or one-off-override with commands.
- **Receive** — every message your Telegram account gets (any group, channel,
  or DM) is posted into the room, tagged with its source.
- Supports **text, images, video, audio, files** both ways; a Telegram sticker
  relays as one line, `【sticker】😀` (stickers are .webp/.tgs, which most Matrix
  clients cannot render at all).
- Per-source **mute** (still shown, but no notification) and **read** commands.

Uses a real account via **MTProto (Telethon)**, so it can reach anyone/anything
the account can — no bot limitations.

Built with a **hexagonal (ports-and-adapters) architecture**: the domain core
imports no SDK. Going from one-way-bot to two-way-user-account only added
adapters and swapped the app wiring — the ports never changed.

## Quick start

Docker is the supported path — the two logins are interactive and run in the
same image, so nothing has to be installed on the host but Docker.

```bash
git clone <your-fork-url> matrix-telegram-bridge
cd matrix-telegram-bridge

cp config.example.yaml config.yaml     # homeserver, user_id, control_room
cp .env.example .env                   # TELEGRAM_API_ID / TELEGRAM_API_HASH

docker compose build                   # build the image

# one-time: sign in to Telegram (phone code, then 2FA if enabled)
docker compose run --rm --entrypoint python bridge \
    -m bridge.tglogin --config /config/config.yaml

# one-time: sign in to Matrix (mints a token the bridge itself owns)
docker compose run --rm --entrypoint python bridge \
    -m bridge.mxlogin --config /config/config.yaml

docker compose up -d
docker compose logs -f
```

A healthy start logs the egress path, `telegram accounts online: N`, and
`matrix sync starting as @you:server`. Then type `!tg help` in your control
room. Details in [Prerequisites](#prerequisites) and
[Run with Docker](#run-with-docker-recommended); everything the bridge writes
lives in `./tgdata`, which is bind-mounted to `/data`.

### Everyday commands

```bash
docker compose logs -f                 # follow the log
docker compose restart                 # after editing config.yaml or /data
docker compose up -d --build           # after changing the code
docker compose up -d --force-recreate  # after editing .env (restart won't reload it)
docker compose down                    # stop; ./tgdata keeps your sessions
```

`docker compose restart` deliberately does **not** re-read `.env`: compose
injects those at container-create time. Anything under `/data` (credentials,
state, sessions) *is* picked up by a plain restart, which is why credentials
live there rather than in `.env`.

## Architecture

```
                     ┌──────────────────────────── core (pure) ───────────────────────────┐
 Element room ──▶ MatrixSource ──▶ Dispatcher ──▶ TelegramUserSink ──▶ Telegram
 (you type)                         │  commands + active-target routing        (send as you)
                                    │  BridgeState (active target, mutes)
 Element room ◀── MatrixSink  ◀── Relay ◀────────  TelegramUserSource ◀── Telegram
 (you read)                          label + mute                             (incoming)
                     └────────────────────────────────────────────────────────────────────┘
```

| Layer | Files | Depends on |
|-------|-------|------------|
| Domain models | `bridge/core/models.py` | nothing |
| Ports (interfaces) | `bridge/core/ports.py` | models |
| Core logic | `bridge/core/{dispatcher,relay,state,transformer}.py` | ports + models |
| Adapters | `bridge/adapters/{matrix_source,matrix_sink,telegram_user_sink,telegram_user_source}.py` | ports + their SDK |
| Config | `bridge/config.py` | models |
| Composition root | `bridge/__main__.py` | everything (only place wired) |

Two clients are shared to avoid double logins: MatrixSource owns the nio client
and MatrixSink borrows it; one Telethon client backs the Telegram sink, source
and directory. Neither direction can loop: every message the bridge posts into
Matrix is stamped with a hidden `space.bridge.origin` key, and MatrixSource
skips anything carrying it (and only ever acts on the owner account's messages).

## In-room commands

Type these in the control room (prefix configurable, default `!tg`):

| Command | What it does |
|---------|--------------|
| `!tg list` | List every Telegram chat your account is in, grouped by kind |
| `!tg dms [N]` | Inbox view of **private chats only** — unread first, with a one-line preview of the last message |
| `!tg room <target>` | Pre-create the dedicated room for a chat (normally created lazily — see below) |
| `!tg rooms` | List every chat ↔ room mapping |
| `!tg dm <target> [N]` | Read one DM's messages (default 20). Lookup is scoped to private chats, so a same-named group can't win |
| `!tg use <name \| @username \| id>` | Set the current send target |
| `!tg who` | Show the current send target |
| `!tg read <target> [N]` | Show the last N messages from a chat (default 10) |
| `!tg info <target>` | Show a user/group/channel's details (id, bio/description, member count, a user's linked channel). In a per-chat room the target may be omitted — it describes that room's chat and refreshes its topic |
| `!tg join <@username \| invite-link>` | Join a public group/channel or a `t.me/+…` invite link |
| `!tg prefix <symbol>` | Change the command prefix (default `!tg`); persisted |
| `!tg accounts` | List logged-in **Telegram accounts** ([details](#multiple-telegram-accounts)): name, id, bound Space |
| `!tg login <phone>` | Log in a new Telegram account; run it in a Space's room to bind that Space |
| `!tg code <code>` / `!tg 2fa <password>` | Continue the login (the command is redacted immediately) |
| `!tg switch <n\|account>` | Change which account is **current**; all of them stay online |
| `!tg bind [account]` / `unbind` | Bind an account to this room's Space, or unbind |
| `!tg logout [account] confirm` | Sign out at Telegram, delete its session and caches, drop it |
| `!tg stats` | Which chats you have records in, and how many |
| `!tg settings` | Dump every current setting |
| `!tg watch <group/channel>` / `unwatch` | Relay allow-list — DMs relay by default; groups and channels need watching, or a dedicated room |
| `!tg watching` | List the allow-list |
| `!tg mute <target>` / `unmute` / `muted` | Choose which sources notify you (muted ones are still shown) |
| `!tg at <YYYY-MM-DD> <HH:MM[:SS]> <msg>` | Send to the current target at an absolute time |
| `!tg fmsg [Normal\|QuotLy]` | Show or set how outgoing text is rendered — as typed, or as a [QuotLy](#quotly-send-mode) quote sticker |
| `!tg delay [<fixed> [random]]` | Show or set an outgoing send delay, e.g. `delay 5s 30s` (`0` disables) |
| `!tg selfdestruct [<kind> <duration>]` | Auto-delete your relayed messages on Telegram after a TTL, per kind (the Matrix copy is marked, not deleted) |
| `!tg delMsg <target\|AllUser\|AllGroup\|AllChannel\|AllChat>` | Delete **your own** messages — irreversible, needs a `confirm` token (no target needed in a per-chat room, see below) |
| `!tg help` | Show help (in Chinese, as are the in-room replies) |
| `@<target> <message>` | Send one message to `<target>` without changing the current one |
| *(plain message / image)* | Send to the current target |
| *(Element reply to a relayed message)* | Routed back to that Telegram chat as a reply |

Incoming Telegram messages appear as `[chat] sender: text`. Muted sources are
posted as `m.notice` (shown, but clients don't notify).

**Reply threading syncs both ways.** When someone on Telegram replies to someone
else — not just to you — the relayed copy is a native Matrix reply
(`m.in_reply_to`), so Element shows the same quote block and a group
conversation keeps its threads instead of arriving flattened. The replied-to
message has to be one the bridge already relayed *into the same room*;
otherwise it relays as a plain message. Belonging to a forum topic is not a
reply, so topics don't get threaded under their first post.

**Deletion and edit sync.** Deleting a relayed message in Element deletes it on
Telegram. In the other direction the Matrix copy is **kept, not destroyed** —
the bridge exists to preserve a readable record:

| On Telegram | In Matrix |
|---|---|
| Someone deletes a text message | Edited in place to `🗑️ ~~original text~~ （已被删除）` |
| Someone deletes a photo/file | A `🗑️ 已删除` note is anchored to it as a reply (replacing it would destroy the file) |
| Someone edits a message | The Matrix message is **edited in place** (`m.replace`), its body showing the new text plus `✏️ 原：~~old text~~` |
| Someone edits a photo caption | A `✏️ 已编辑` note listing old and new (replacing would drop the file) |
| **A self-destruct TTL expires** | Marked the same way — **never redacted**, so the record stays readable |

Self-destruct runs through the very same code path as someone else's deletion,
so the two are indistinguishable in Matrix: gone from Telegram, struck through
here.

**Deletions that overtake their own message are not lost.** Spam is often
deleted within the same second it is posted, so the delete update can arrive
before the bridge has finished relaying the message and writing its link.
Dropping it is what made deletion sync look intermittent. Now an unmatched
delete leaves a tombstone (15 minutes, only for chats that actually relay), and
the mark is applied the moment the message shows up:

```
delete pending: no link yet for chat … msg …  (will apply if it arrives)
applying delete that arrived before the link: …
```

Deletions in chats that are not relayed drop to debug level — every group the
account belongs to deletes messages constantly, and that was pure noise at INFO.

Edits use Matrix's native edit, so they add no extra events while keeping both
versions visible. Across repeated edits the **first** original is what's shown,
not the previous version. The rendered prefix (`[chat] sender`) is restored too,
so an edit doesn't reformat the line.

**Only messages with a link are covered.** Links are written when a message is
relayed, so messages relayed before this feature shipped aren't supported. When
a sync is skipped the reason is logged:

```
delete ignored: no link for chat -100… msg 1047906
edit ignored: no link for chat … msg …
```

The tg↔matrix link is persisted (`/data/msglinks.json`, bounded by size and
age), so this survives restarts. Two platform limits worth knowing: Telegram's
delete update for DMs and basic groups doesn't name the chat (resolved via
account-wide-unique message ids), and neither works for messages the bridge
never relayed. Edits that don't change the text — Telegram "edits" a message
when it attaches a link preview — are ignored.

The allow-list matters: without it, busy channels flood the room hard enough to
hit matrix.org rate limits.

## QuotLy send mode

`!tg fmsg QuotLy` changes what lands in the target chat: instead of your text,
a quote **sticker** rendered by [@QuotLyBot](https://t.me/QuotLyBot). Per
account, persisted, and `!tg fmsg Normal` puts it back.

Telegram has no API for this, so the account does what a person would:

1. sends the text to the bot,
2. waits for the sticker it answers with,
3. sends that sticker to the real target, and
4. deletes both messages (yours and the bot's) from the bot chat, for both
   sides — the round trip leaves nothing behind.

Worth knowing:

* **Text only.** Images, files and captions are sent as they always were —
  only a plain typed message is quoted.
* **It never loses a message.** If the bot is slow, silent, or answers with
  something that isn't a sticker, the message is sent as typed and the reason
  is logged. An unstyled message beats a missing one.
* **Everything else still applies.** Send delay, `!tg at`, self-destruct,
  reply threading and delete-sync all work — they act on the sticker, since
  that is the message that actually exists in the chat.
* **Don't `watch` @QuotLyBot.** Watching it would relay this traffic into
  Matrix for the second or two before it is deleted.
* **The bot sees what you quote.** It has to, to render it. That is one more
  party than a normal send, so it is opt-in and off by default.

The bot is configurable, for a fork or a self-hosted clone:

```yaml
options:
  quotly_bot: "@QuotLyBot"
```

## Per-chat rooms (Space mode)

With `matrix.space` set to a Space id, every Telegram conversation gets its
**own Matrix room** inside that space instead of everything sharing one room:

```yaml
matrix:
  space: "!yourSpace:matrix.org"   # create a Space in Element, paste its id
```

- Rooms are created **lazily** — the first relayed message from a chat creates
  its room (named e.g. `👤 Alice`, filed under the space). `!tg room <target>`
  pre-creates one; `!tg rooms` lists the mappings (persisted in
  `/data/rooms.json`).
- **A dedicated room is itself the opt-in**: `!tg room` on a group or channel
  makes it relay without being watched — a room that receives nothing is just
  confusing. `unwatch` therefore won't silence such a chat, and says so; use
  `!tg mute` for quiet.
- **Typing in a per-chat room sends straight to that chat** — no prefix, like a
  real chat window. Replies, delete-sync, send-delay and self-destruct all work
  per-room. `!tg` commands typed there are intercepted with a pointer to the
  control room (so a stray `!tg mute` never reaches a human).
- Two commands do work there, both scoped to **that room's chat** so no target
  is typed: `!tg info` shows its details, and `!tg delMsg confirm` deletes all
  of your own messages in it (without `confirm` it only asks; a target typed
  anyway is ignored — it always means this room's chat).
- Inside a DM's room the `[chat] sender:` prefix is dropped entirely; group and
  channel rooms keep the sender only.
- Each room's **topic** is filled with that chat's info (description, id, member
  count; for a user: bio and linked channel) at creation, and refreshed when you
  run `!tg info` in the room.
- The control room keeps all commands and `@target` sends, and is the
  **fallback**: if room creation fails (rate limit), the message lands there —
  degraded layout, never a lost message.
- The bridge must have permission to add children to the space (it does if the
  space was created by the bridge account; otherwise grant it moderator).

Leave `space` blank for the old single-room behaviour.

## Prerequisites

- A **Matrix account** for the bridge (this is also the account you type as in
  the control room), joined to the control room.
- Its **password** — `bridge.mxlogin` uses it to mint the bridge's own token.
  (An **access token** from Element ▸ *Settings ▸ Help & About ▸ Access Token*
  also works via `--token`, but dies when that Element session logs out.)
- The control **room's internal id** — Element ▸ *Room Settings ▸ Advanced ▸
  Internal room ID* (`!…:server`).
- Telegram **api_id / api_hash** from <https://my.telegram.org> ▸ *API
  development tools*.

## Configure

```bash
cp config.example.yaml config.yaml   # homeserver, user_id, control_room
cp .env.example .env                  # MATRIX_ACCESS_TOKEN, TELEGRAM_API_ID/HASH
```

Env vars override the file (preferred for secrets): `MATRIX_HOMESERVER`,
`MATRIX_USER_ID`, `MATRIX_CONTROL_ROOM`, `MATRIX_ACCESS_TOKEN`,
`MATRIX_PASSWORD`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`.

## Run with Docker (recommended)

The Telegram login is interactive, so do it once via `docker compose run`; it
writes the session into `./tgdata` (which also holds the Matrix store + state).

```bash
docker compose build

# 1) one-time Telegram login — enter the code Telegram sends you (and 2FA)
docker compose run --rm --entrypoint python bridge \
    -m bridge.tglogin --config /config/config.yaml

# 2) one-time (or any-time) Matrix login — see "Changing the Matrix account"
docker compose run --rm --entrypoint python bridge \
    -m bridge.mxlogin --config /config/config.yaml

# 3) run
docker compose up -d
docker compose logs -f      # look for "telegram authorised as ..." + "matrix sync starting"
```

## CLI reference

Two command-line tools, both interactive, both run **where the bridge runs**:

| Tool | Purpose | How often |
|------|---------|-----------|
| `bridge.tglogin` | Log the Telegram user account in (phone code + 2FA), writes `telegram.session` | once |
| `bridge.mxlogin` | Set or change the Matrix account, writes `matrix_creds.json` | any time |

### Invoking them

```bash
# Docker (the normal case) — run from the directory holding docker-compose.yml
docker compose run --rm --entrypoint python bridge -m bridge.mxlogin --config /config/config.yaml
docker compose run --rm --entrypoint python bridge -m bridge.tglogin --config /config/config.yaml

# No Docker
python -m bridge.mxlogin --config config.yaml
```

For a **remote** bridge, use the wrapper rather than SSHing by hand — it runs
the login on the server, so the homeserver logs the server's address and never
your laptop's, and the password stays inside the SSH tunnel:

```bash
./scripts/mxlogin.sh root@vps.example.com            # POSIX shell
.\scripts\mxlogin.ps1 -Server root@vps.example.com   # PowerShell
```

Set these once and the wrapper needs no arguments at all:

```powershell
[Environment]::SetEnvironmentVariable("BRIDGE_SSH_HOST",    "root@vps.example.com", "User")
[Environment]::SetEnvironmentVariable("BRIDGE_REMOTE_PATH", "/srv/matrix-telegram-bridge", "User")
```

```bash
export BRIDGE_SSH_HOST=root@vps.example.com
export BRIDGE_REMOTE_PATH=/srv/matrix-telegram-bridge
```

### `bridge.mxlogin` flags

| Flag | Effect |
|------|--------|
| `--config PATH` | Config file (default `$BRIDGE_CONFIG`, else `config.yaml`) |
| `--homeserver URL` | Skip the homeserver prompt |
| `--user @name:server` | Skip the user-id prompt (ignored in token mode — `/whoami` decides) |
| `--room !id:server` | Skip the control-room prompt; a `#alias:server` is resolved |
| `--device-name NAME` | Device label shown at the homeserver (default `MATRIX_TG_BRIDGE`) |
| `--token` | Use an existing access token instead of a password (SSO accounts) |
| `--token-stdin` | Read that token from stdin — implies `--token` |
| `--password-stdin` | Read the password from stdin instead of prompting |
| `--no-egress-check` | Skip the outbound-IP lookup (avoids one third-party request) |
| `-y`, `--yes` | Assume yes for confirmations |
| `-h`, `--help` | Usage |

Exit codes: `0` ok · `1` declined at a prompt · `2` proxy unusable · `3` auth
rejected · `4` control room unreachable.

`bridge.tglogin` takes only `--config`; everything else it asks for (phone,
code, 2FA password).

### What a run looks like

```
   _  _   __   ____  ____  __  _  _
  ( \/ ) /__\ (_  _)(  _ \(  )( \/ )
   )  ( /(__)\  )(   )   / )(  )  (
  (_/\_)__)(__)(__) (_)\_)(__)(_/\_)

   a c c o u n t   l o g i n   ::   tg-bridge

[ proxy ]-----------------------------------------------------
  [ok]   using socks5h://127.0.0.1:1080 (from system)
  [ok]   address the homeserver will log: 146.70.134.171

[ account ]---------------------------------------------------
  homeserver [https://matrix.org]:        <- Enter accepts the default

  how do you want to authenticate?
    1) password  - mints a NEW token owned by the bridge (recommended)
    2) token     - paste an existing access token (SSO accounts)
  choice [1]:

[ login ]-----------------------------------------------------
  password (not echoed):
  [ok]   new device: ABCD1234 ('MATRIX_TG_BRIDGE')
  [ok]   authenticated as @you:matrix.org

[ control room ]----------------------------------------------
  [ok]   control room joined: !yourRoom:matrix.org

[ store ]-----------------------------------------------------
  [ok]   credentials written to /data/matrix_creds.json
```

The egress line is a safety check, not decoration: confirm it is the address
you expect **before** typing a password. If it shows your real ISP address when
it should show a VPN exit, answer `n`.

### Two things that will bite you

**Don't run it from Git Bash / MSYS.** Those rewrite `/config/config.yaml` into
`C:/Program Files/Git/config/config.yaml`. Use PowerShell, or prefix the
command:

```bash
MSYS_NO_PATHCONV=1 ./scripts/mxlogin.sh
```

**`-T` disables the TTY**, so the hidden password prompt gets EOF and exits.
Only use `-T` together with the scripted flags below.

### Scripted (no prompts)

Secrets go over **stdin, never argv** — argv is visible to other processes and
lands in shell history. The piped line is consumed before any prompt runs, so
it cannot be mistaken for an answer:

```bash
printf '%s\n' "$TOKEN" | docker compose run --rm -T --entrypoint python bridge \
    -m bridge.mxlogin --config /config/config.yaml \
    --token-stdin --room '!yourRoom:matrix.org' -y
```

Questions that have a configured default answer themselves when there is no
tty; anything without one must be passed as a flag.

## Changing the Matrix account

`bridge.mxlogin` authenticates interactively and writes the result to
`/data/matrix_creds.json`, which overrides the homeserver / user id / token /
control room in `config.yaml` and `.env`. It offers two ways in:

| | how | when |
|---|---|---|
| **password** (default) | logs in, mints a token for a device the *bridge* owns | normal case — survives Element logouts |
| **token** (`--token`) | adopts an access token you paste, verified via `/whoami` first | SSO-only accounts with no password |

The token path derives the user id and device id from `/whoami` rather than
trusting what was typed — a token that disagrees with the account it claims to
be would otherwise fail much later, as unexplained silence in sync.

See [CLI reference](#cli-reference) for how to invoke it and every flag.

**No restart needed.** The running bridge hashes `matrix_creds.json` every 2s;
when it changes it tears down the wiring and rebuilds against the new account
in about a second. The trigger is the file itself, which is why this works even
though `mxlogin` runs in a *separate* container — they share the `/data` mount.

If the new configuration fails to load, the bridge logs the error and keeps
running on the previous account rather than going down.

Two things this fixes for good:

* Credentials live on the data volume, not in `.env`, so a change needs
  `restart` rather than `up --force-recreate`.
* The token belongs to the bridge's own device, so logging out of an Element
  session no longer kills the bridge with `M_UNKNOWN_TOKEN`.

### Changing the control account

Only through `bridge.mxlogin`, run on the host. Matrix stays **one account** —
multiple accounts are a Telegram-side thing, see below.

Switching to a *different* user id destroys the previous account's caches:
`msglinks.json` (relayed text), `rooms.json` (room map), `outbox.json` (queued
sends), the nio store (sync position and device keys), `state.json`'s
room→target map, and any `<file>.tmp` left by a process killed mid-write (those
hold a full copy). Telegram accounts, sessions, mutes and the watch list are
kept — that side did not change.

## Multiple Telegram accounts

**One Matrix account controls the bridge; several Telegram accounts run under
it at the same time.** Each Telegram account:

- is bound to **one Matrix Space**, where all its per-chat rooms are created;
- owns its session file and caches (`accounts/tg-<id>/`), invisible to the others;
- is **online concurrently** — this is not "switch between accounts", every
  account relays into its own Space simultaneously.

```
!tg accounts                     list them: name, id, bound Space, ⭐ current
!tg login +8613800138000         log in a new account (see below)
!tg switch 2                     change which one is "current"
!tg bind / unbind                bind or unbind a Space
!tg logout <account> confirm     forget one
```

"Current" only decides who **control-room** commands and sends belong to.
Typing in a per-chat room always goes through that room's own account,
whichever one is current.

### Logging in from inside the Space

Create the Space in Element, invite the bridge account, then in **any room
inside that Space**:

```
!tg login +8613800138000
!tg code 12345
!tg 2fa <two-factor password>     (only if the account has 2FA)
```

The Space is bound to the new account automatically — no room ids to copy.
`space=<id>` overrides it, and `!tg bind` in a Space's room binds an account
that already exists.

Account commands (accounts / login / code / 2fa / switch / bind / logout) work
in **any room the bridge is in**, which is what makes the above possible. Every
other command still only works in the control room.

> ⚠️ **The code and 2FA password enter room history.** The bridge redacts the
> command immediately, but with the same limits described for the Matrix
> password: the server has already received it, and redaction only removes an
> event's content. To avoid the exposure entirely, log in on the host:
> `docker compose run --rm --entrypoint python bridge -m bridge.tglogin --config /config/config.yaml --space '!spaceid:matrix.org'`
> Both paths write the same account list, so `!tg accounts` shows either.

### switch vs logout

| | does | destructive |
|---|---|---|
| `switch` | change which account is "current" | **no** — every account stays online and its rooms keep working |
| `logout` | sign out at Telegram, delete the session file and caches, drop it from the list | **yes**, hence `confirm` |

`logout` signs the session out at Telegram too: deleting only the local session
file would leave an authorised device on the account for ever.

### Data layout

```
/data/
  matrix_creds.json          the Matrix control account (reload trigger)
  telegram_accounts.json     the Telegram account list (mode 600)
  state.json                 global settings: mutes, watch list, delay, TTLs
  store/                     Matrix sync store
  accounts/
    tg-1234567890/           telegram.session · rooms.json · msglinks.json
    tg-7788990011/           outbox.json · expire.json   <- invisible to each other
```

Accounts share **no caches at all**, so one account's relayed text and room map
cannot reach another. That is structural, not a matter of remembering to purge.

Upgrading from the single-account version adopts the existing
`telegram.session` as account #1, turns `matrix.space` into its bound Space, and
moves the existing room map and message links into its directory (the log says
what moved). Nothing has to be logged in again, and no room is re-created.

#### What none of this reaches

Even logout is local hygiene, not a guarantee about every copy:

* **Messages already relayed into Matrix rooms stay on the homeserver** (and on
  any federated server that received them). To clear them, delete or leave
  those rooms in Element.
* Content already synced to other clients, push notifications, server backups
  and federated copies are outside anything the bridge can touch.
* Redaction removes an event's content, not the fact of the event.


## Proxy / privacy

One proxy covers both sides — Matrix (via an `aiohttp-socks` connector) and
Telegram (via `python-socks`):

```yaml
proxy:
  url: "system"    # default: inherit the machine's proxy
  # url: "socks5h://127.0.0.1:1080"   # explicit (or BRIDGE_PROXY in .env)
  # url: "none"                        # deliberately direct
```

`system` (the default, and what a blank value means) reads `ALL_PROXY` /
`HTTPS_PROXY` / `HTTP_PROXY`, then the WinINET registry setting on Windows.
Startup logs which path traffic actually takes:

```
egress: via socks5h://127.0.0.1:1080 (from system)
egress: DIRECT - no system proxy found (expected behind a full-tunnel VPN; ...)
```

### With a full-tunnel VPN (WireGuard / Mullvad)

A full-tunnel VPN is **not** a proxy — it captures traffic at the network
layer and sets no system proxy at all, so `system` correctly finds nothing and
`DIRECT` genuinely means "through the tunnel". Leave the proxy unset.

The thing actually worth verifying is whether Docker's NAT escapes the tunnel.
Check it from inside the container rather than assuming:

```bash
docker compose run --rm --entrypoint python bridge -c \
  "import json,urllib.request;print(json.load(urllib.request.urlopen('https://am.i.mullvad.net/json')))"
# mullvad_exit_ip: True  -> container traffic is inside the tunnel
```

Mullvad also runs a SOCKS5 proxy reachable *only* from inside its WireGuard
tunnel (`socks5h://10.64.0.1:1080`) if you would rather pin egress explicitly
than rely on default routing.

* Prefer `socks5h://` over `socks5://` — the `h` resolves DNS **at the proxy**,
  so your resolver never sees the destinations. The bridge warns if you use the
  local-DNS form.
* **Fail closed:** if a proxy is configured but cannot be applied (bad URL,
  missing transport package), the bridge and the CLI exit rather than silently
  connecting direct.
* MTProto cannot ride an HTTP proxy — Telegram needs SOCKS.
* `telegram.device_model` / `system_version` / `app_version` are pinned in
  config so Telethon reports a generic client instead of this host's real
  `uname`.
* `mxlogin` shows the outbound address a remote server will attribute the login
  to before you type a password (`--no-egress-check` skips that lookup).

## Run locally (no Docker)

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows;  source .venv/bin/activate on *nix
pip install -r requirements-dev.txt

python -m bridge.tglogin --config config.yaml   # once, interactive
python -m bridge.mxlogin --config config.yaml   # Matrix account, any time
python -m bridge --config config.yaml
```

*(For local runs set `store_path`, `session`, `state_path` to local paths like
`./store`, `./telegram.session`, `./state.json` instead of `/data/...`.)*

## Running the tests

The suite needs **no network, no credentials and no Telegram/Matrix account** —
every port is driven through in-memory fakes, so it is safe to run on a clean
checkout before you configure anything.

```bash
# In Docker (nothing to install; Dockerfile.test is the dev image)
docker build -f Dockerfile.test -t mtb-test .
docker run --rm -v "$PWD:/src" mtb-test python -m pytest -q

# Locally
pip install -r requirements-dev.txt
pytest -q
```

469 tests, about 4 seconds. Useful variants:

```bash
pytest -q tests/test_relay.py          # one file
pytest -q -k forward                   # by name
pytest -q -x -vv                       # stop at the first failure, verbose
```

On Windows the Docker mount needs an absolute path — in PowerShell use
`-v "${PWD}:/src"`, in Git Bash `MSYS_NO_PATHCONV=1 docker run ... -v "/$(pwd):/src"`.

## Testing strategy

The whole control/relay pipeline is tested through the ports with in-memory
fakes — **no network, no SDKs**:

- `test_dispatcher.py` — commands + active-target / `@`-override routing
- `test_relay.py` — labelling, mute→silent, media fetch + fallback
- `test_state.py` — active target & mute persistence
- `test_matrix_sink.py` — msgtype, loop-guard stamp, media upload (fake nio)
- `test_telegram_user_sink.py` — send_message/send_file mapping (fake Telethon)
- `test_proxy.py` / `test_system_proxy.py` — proxy URL parsing, system-proxy
  detection, fail-closed behaviour
- `test_creds.py` — credential round-trip, precedence, corrupt-file degradation
- `test_hot_reload.py` — credential-change detection by content hash
- `test_mxlogin.py` — stdin secret handling, auth-method choice, no-tty defaults
- `test_supervisor.py` — a fatal startup error is logged and exits non-zero;
  the account switch re-purges once the previous app has stopped
- `test_messagelinks.py` — the tg↔matrix link store: exact vs msg-id lookup,
  the event-id reverse index, eviction, and flushing on close
- `test_expirer.py` — self-destruct marks the Matrix copy instead of redacting it
- `test_purge.py` — what a wipe destroys, and what it must not touch
- `test_accounts.py` — the Telegram account vault, and adopting a
  single-account install without losing its room map
- `test_account_commands.py` — `!tg accounts / login / code / 2fa / switch /
  bind / logout`: the code is redacted and never echoed, a Space is bound from
  the room the command was typed in, and an offline account stays manageable
- `test_ordering_and_deleted.py`, `test_new_commands.py`, `test_rooms.py`,
  `test_dms.py`, `test_bots_and_avatars.py`, `test_matrix_rooms.py` — per-chat
  room routing, deletion/edit marking, room creation, avatars and bot filtering
- `test_outbound_scheduler.py`, `test_replymap.py`, `test_duration.py`,
  `test_telegram_user_source.py` — send delay/scheduling and retries, reply
  mapping, duration parsing, and reading Telethon reply/forward headers
- `test_transformer.py`, `test_config.py` — pure helpers & config validation

## Forking / publishing

Everything that identifies you stays out of the repo by design — `.gitignore`
excludes `.env`, `config.yaml`, `tgdata/`, `store/` and `*.session*`. Only the
`.example` files are tracked, and they contain nothing but placeholders.

Before pushing a fork, confirm that yourself:

```bash
git status --ignored --short          # your real files should be listed as ignored
git ls-files | grep -Ei 'env|session|config\.yaml'   # expect only the .example ones
```

Three things worth knowing:

- **Never commit `config.yaml`.** It holds your Matrix user id, control room
  and Space ids — enough to identify the account even without the token.
- **`tgdata/` is the sensitive one.** `msglinks.json` stores the *text* of
  relayed messages, and `rooms.json` maps Telegram chat ids to Matrix rooms.
  Keep it out of the repo and out of any backup you share.
- **Rotate anything that has ever been committed.** A token or `api_hash` that
  reached a public commit stays in the history after you delete it — mint a new
  one at <https://my.telegram.org> / in Element instead.

## Notes & limitations

- Targets **unencrypted** Matrix rooms. E2EE needs `libolm` + attachment
  decryption; left out to keep the image slim.
- The control room should be **private to you**: the bridge acts on the owner
  account's messages, and shows all your Telegram traffic there.
- On startup, Matrix backlog is ignored (only new messages act as commands).
- Delivery is best-effort per message; failures are logged, not fatal.
- Using a user account for automation is subject to Telegram's terms — keep a
  normal, non-abusive sending pattern.
