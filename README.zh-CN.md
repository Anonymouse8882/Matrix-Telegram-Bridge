# Matrix ⇄ Telegram 双向桥（交互式）

*[English](README.md)*

在一个 Matrix（Element）房间里操控你的**真实 Telegram 账号**：

- **发送** —— 在房间里打字就发到当前选定的 Telegram 对话；可以随时切换目标，
  也可以临时指定一次。
- **接收** —— 你的 Telegram 账号收到的消息会转发进这个房间，并标注来源。
- 双向支持**文字、图片、视频、语音、文件**；Telegram 贴纸转成一行 `【sticker】😀`
  （贴纸是 .webp/.tgs，多数 Matrix 客户端根本渲染不出来）。
- 按来源**静音**（仍显示，但不提醒）和 **read** 查看历史。

用的是 **MTProto（Telethon）**的真实账号，不是 bot——账号能触达的对象它都能触达，
没有 bot 的各种限制。

采用**六边形（端口与适配器）架构**：领域核心不 import 任何 SDK。从"单向 bot"演进到
"双向用户账号"只新增了适配器并换了装配代码，端口定义一行没动。

## 快速开始

推荐用 Docker：两次登录都是交互式的，且跑在同一个镜像里，所以宿主机上除了
Docker 什么都不用装。

```bash
git clone <你的仓库地址> matrix-telegram-bridge
cd matrix-telegram-bridge

cp config.example.yaml config.yaml     # homeserver、user_id、control_room
cp .env.example .env                   # TELEGRAM_API_ID / TELEGRAM_API_HASH

docker compose build                   # 构建镜像

# 一次性：登录 Telegram（输入验证码，开了两步验证再输密码）
docker compose run --rm --entrypoint python bridge \
    -m bridge.tglogin --config /config/config.yaml

# 一次性：登录 Matrix（铸造一个属于桥自己的 token）
docker compose run --rm --entrypoint python bridge \
    -m bridge.mxlogin --config /config/config.yaml

docker compose up -d
docker compose logs -f
```

启动正常时日志会打印出口路径、`telegram accounts online: N`，以及
`matrix sync starting as @you:server`。然后在控制房间里发 `!tg help`。
细节见[前置条件](#前置条件)和[用 Docker 运行](#用-docker-运行推荐)；
桥写入的所有数据都在 `./tgdata`，它被挂载到容器里的 `/data`。

### 日常命令

```bash
docker compose logs -f                 # 跟踪日志
docker compose restart                 # 改了 config.yaml 或 /data 里的东西之后
docker compose up -d --build           # 改了代码之后
docker compose up -d --force-recreate  # 改了 .env 之后（restart 不会重新加载）
docker compose down                    # 停止；./tgdata 里的会话会保留
```

`docker compose restart` **不会**重新读取 `.env` —— compose 是在创建容器时把这些
变量注入进去的。而 `/data` 下的东西（凭据、状态、会话）普通 restart 就能生效，
这正是凭据放在那里而不是放 `.env` 的原因。

## 架构

```
                     ┌──────────────────────────── 核心（纯逻辑） ─────────────────────────┐
 Element 房间 ──▶ MatrixSource ──▶ Dispatcher ──▶ TelegramUserSink ──▶ Telegram
 （你输入）                         │  命令解析 + 当前目标路由                （以你的身份发送）
                                    │  BridgeState（当前目标、静音表）
 Element 房间 ◀── MatrixSink  ◀── Relay ◀────────  TelegramUserSource ◀── Telegram
 （你阅读）                        打标签 + 静音过滤                            （收到的消息）
                     └────────────────────────────────────────────────────────────────────┘
```

| 层 | 文件 | 依赖 |
|----|------|------|
| 领域模型 | `bridge/core/models.py` | 无 |
| 端口（接口） | `bridge/core/ports.py` | 模型 |
| 核心逻辑 | `bridge/core/{dispatcher,relay,state,transformer}.py` | 端口 + 模型 |
| 适配器 | `bridge/adapters/{matrix_source,matrix_sink,telegram_user_sink,telegram_user_source}.py` | 端口 + 各自的 SDK |
| 配置 | `bridge/config.py` | 模型 |
| 装配根 | `bridge/__main__.py` | 全部（唯一接线处） |

两个客户端共享以避免重复登录：MatrixSource 持有 nio 客户端，MatrixSink 借用；
一个 Telethon 客户端同时支撑 Telegram 的发送、接收和目录查询。两个方向都不会成环：
桥发进 Matrix 的每条消息都带一个隐藏字段 `space.bridge.origin`，MatrixSource 会跳过
带这个标记的消息，并且只响应你本人账号发的消息。

## 房间内命令

在控制房间里输入（前缀可配置，默认 `!tg`）：

| 命令 | 作用 |
|------|------|
| `!tg list` | 列出账号所在的全部 Telegram 对话（按私信/群组/频道分组） |
| `!tg dms [N]` | **只看私信**的收件箱视图 —— 未读优先，每个附带最后一条消息的一行摘要 |
| `!tg room <目标>` | 手动预建某对话的专属房间（平时会懒创建，见下节） |
| `!tg rooms` | 列出全部 对话↔房间 映射 |
| `!tg dm <目标> [N]` | 查看某个私信的内容（默认 20 条）。目标只在私信范围内解析，同名的群不会被误选 |
| `!tg use <名称 \| @用户名 \| id>` | 设为当前发送目标 |
| `!tg who` | 显示当前发送目标 |
| `!tg read <目标> [N]` | 查看某对话最近 N 条消息（默认 10） |
| `!tg info <目标>` | 查看用户/群组/频道信息（id、简介/bio、成员数、用户的个人频道）。在专属房里可省略目标，直接看该房对应对话并刷新房间主题 |
| `!tg join <@用户名 \| 邀请链接>` | 加入公开群组/频道，或 `t.me/+…` 邀请链接 |
| `!tg prefix <符号>` | 自定义命令前缀（默认 `!tg`），持久化 |
| `!tg accounts` | 列出已登录的 **Telegram 账户**（名字 + id + 绑定的空间） |
| `!tg login <手机号>` | 登录一个新 TG 账户；在目标空间的房间里发即自动绑定该空间 |
| `!tg code <验证码>` / `!tg 2fa <密码>` | 按提示继续登录（命令消息会被立即撤回） |
| `!tg switch <序号\|账户>` | 切换**当前**账户；所有账户依然同时在线 |
| `!tg bind [账户]` / `unbind` | 把账户绑到本房间所在的空间 / 解绑 |
| `!tg logout [账户] confirm` | 退出：TG 端注销 + 删会话和缓存 + 移出列表 |
| `!tg stats` | 你在哪些对话有记录、各多少条 |
| `!tg settings` | 打印当前所有设置 |
| `!tg watch <群/频道>` / `unwatch` | 接收白名单增删（**私信默认转发；群组和频道要加白名单，或者有专属房间**） |
| `!tg watching` | 查看接收白名单 |
| `!tg mute <目标>` / `unmute` / `muted` | 控制哪些来源提醒你（静音的仍然显示） |
| `!tg at <YYYY-MM-DD> <HH:MM[:SS]> <内容>` | 定时发送到当前目标 |
| `!tg fmsg [Normal\|QuotLy]` | 查看或设置发送模式：原样发送，或转成 [QuotLy 语录贴纸](#quotly-发送模式) |
| `!tg delay [<固定> [随机]]` | 查看或设置发送延迟，如 `delay 5s 30s`（`0` 关闭） |
| `!tg selfdestruct [<类型> <时长>]` | 按类型设置自毁 TTL：到期删 Telegram 那条，Matrix 这边只标记 |
| `!tg delMsg <目标\|AllUser\|AllGroup\|AllChannel\|AllChat>` | 删除**你自己**发的消息，不可逆，需要 `confirm` 令牌（专属房里免写目标，见下） |
| `!tg help` | 显示帮助 |
| `@目标 内容` | 临时发给指定目标，不改变当前目标 |
| *（直接发消息／图片）* | 发给当前目标 |
| *（在 Element 里回复某条转发消息）* | 作为回复发回该 Telegram 对话 |

收到的 Telegram 消息显示为 `[对话] 发送者: 内容`。静音来源以 `m.notice` 形式发出
（显示但客户端不提醒）。

**回复关系双向同步。** Telegram 那边谁回复了谁（不只是你自己的消息），转发到
Matrix 时也是一条原生回复（`m.in_reply_to`），Element 里同样带引用块，群聊里的
对话线索不会被拍平。被回复的那条必须是 bridge 转发过、且落在**同一个房间**里的
消息，否则只当普通消息转发（论坛群里“属于某话题”不算回复，不会把整个话题都串到
话题首帖下面）。

**删除与编辑同步。** 在 Element 里删除转发消息 → Telegram 那边也删。反方向则
**保留不销毁** —— 桥的意义就是留一份可读的记录：

| Telegram 那边 | Matrix 这边 |
|---|---|
| 对方删除文字消息 | 原地编辑为 `🗑️ ~~原文~~ （已被删除）`，内容仍可读 |
| 对方删除图片/文件 | 以回复形式挂一条 `🗑️ 已删除` 提示（替换会毁掉文件本身） |
| 对方编辑消息 | **原地编辑**该条 Matrix 消息（`m.replace`），正文为「新内容 + `✏️ 原：~~旧内容~~`」 |
| 对方编辑图片说明 | 挂一条 `✏️ 已编辑` 提示，同样新旧都列（原地替换会毁掉文件） |
| **自毁到期**（`selfdestruct`） | 同上标记为已删除，**不会真删** —— 记录照样可读 |

自毁走的是和"对方删除"完全相同的那段代码，所以两者在 Matrix 里长得一模一样：
Telegram 那条没了，Matrix 这边留一条划掉的记录。

**删得太快也不会漏。** 广告消息常常发出来一秒内就被管理员删掉，删除事件会赶在
bridge 写完链接之前到达 —— 以前这种就永久丢失了（表现为"有时候删除同步不了"）。
现在查不到链接时会记一个墓碑（15 分钟内有效，只针对会转发的对话），等那条消息真的
转发进来立刻补上标记。日志里是：

```
delete pending: no link yet for chat … msg …  (will apply if it arrives)
applying delete that arrived before the link: …
```

不转发的对话（没加白名单也没专属房）的删除事件降到 debug 级别 —— 账号所在的每个群
时时刻刻都在删消息，那些刷在 INFO 里纯属噪音。

编辑走 Matrix 原生编辑，不会往房间里塞多余消息，新旧内容同时可见。反复编辑时
显示的始终是**最初**那一版，不是上一版。原消息的渲染前缀（`[对话] 发送者`）会一并
还原，格式不走样。

**只对建立了链接的消息生效。** 链接在消息被转发时写入，所以功能上线之前转发过的
旧消息不受支持。同步被跳过时日志里会写明原因：

```
delete ignored: no link for chat -100… msg 1047906
edit ignored: no link for chat … msg …
```

tg↔matrix 映射持久化在 `/data/msglinks.json`（按数量和时间双重上限），重启后依然
有效。两个平台限制：Telegram 对私信和普通群的删除事件不带对话 id（靠账号内全局
唯一的消息 id 反查）；两者都追不了 bridge 从未转发过的消息。文本没变的"编辑"会被
忽略 —— Telegram 给消息附加链接预览时也会触发一次编辑事件。

白名单这条很关键：不加白名单的话，活跃频道刷屏的量足以撞上 matrix.org 的限流。

## QuotLy 发送模式

`!tg fmsg QuotLy` 改变发到目标对话里的东西：不再是你输入的文字，而是由
[@QuotLyBot](https://t.me/QuotLyBot) 渲染出来的语录**贴纸**。按账户保存、持久化，
`!tg fmsg Normal` 改回原样发送。

Telegram 没有对应的 API，所以账号做的事和真人一样：

1. 把文字发给机器人，
2. 等它回一张贴纸，
3. 把贴纸发给真正的目标，
4. 再把和机器人之间的两条消息（你发的 + 它回的）双向删掉 —— 整个来回不留痕迹。

几点需要知道：

* **只对纯文字生效。** 图片、文件、带说明的媒体照旧原样发送。
* **不会丢消息。** 机器人慢、不回、或者回的不是贴纸时，按你输入的原文发送，并在
  日志里写明原因 —— 样式没了总比消息没了强。
* **其他设置照常叠加。** 发送延迟、`!tg at` 定时、自毁、回复关系、删除同步都正常
  工作，作用在贴纸上 —— 因为对话里真实存在的就是那条贴纸。
* **别 `watch` @QuotLyBot。** watch 了的话，那一两秒内的往来消息会被转发进 Matrix。
* **机器人能看到你引用的内容。** 要渲染就必须看到。这比普通发送多了一个第三方，
  所以默认关闭，需要显式打开。

机器人本身可以换（自建或 fork 时用）：

```yaml
options:
  quotly_bot: "@QuotLyBot"
```

## 专属房间（空间模式）

把 `matrix.space` 设为一个空间（Space）的 id 后，每个 Telegram 对话都会在空间里
拥有**自己的 Matrix 房间**，不再全部挤在一个房里：

```yaml
matrix:
  space: "!yourSpace:matrix.org"   # 在 Element 里建一个空间，粘它的内部 id
```

- 房间**懒创建** —— 某对话第一条被转发的消息触发建房（房名如 `👤 Alice`，自动挂进
  空间）。`!tg room <目标>` 手动预建；`!tg rooms` 查看映射（持久化在
  `/data/rooms.json`）。
- **有专属房间 = 已开启转发**：用 `!tg room` 给某个群/频道建了房，它的消息就会转发
  进来，不必再 `watch`（一个收不到任何消息的专属房间只会让人困惑）。反过来，对这种
  对话执行 `unwatch` 不会让它安静下来，命令会明确告诉你这一点 —— 想清静用
  `!tg mute`。
- **在专属房里直接打字就是发给那个对话** —— 不需要任何前缀，像真正的聊天窗口。
  回复、删除同步、发送延迟、自毁都按房间正常工作。在专属房里输入 `!tg` 命令会被
  拦截并提示去全局房（防止手滑把 `!tg mute` 发给真人）。
- 专属房里可用的命令有两个，都作用于**本房对应的那个对话**，不用写目标：
  `!tg info` 看信息、`!tg delMsg confirm` 删掉你在该对话里的全部消息（不带
  `confirm` 时只提示确认；写别的目标也只删本房这个对话）。
- 私信房里不再显示 `[对话] 发送者:` 前缀；群组和频道房只保留发送者名。
- 每个房间的**主题**在建房时填入该对话信息（群/频道：简介、id、人数；用户：bio、
  个人频道），在房内执行 `!tg info` 会刷新。
- 全局房保留全部命令和 `@目标` 临时发送，并且是**兜底**：建房失败（限流等）时
  消息落到全局房 —— 布局降级，消息绝不丢失。
- bridge 需要有往空间里挂子房间的权限（空间就是 bridge 账号建的就没问题；否则
  给它协管员权限）。

`space` 留空即回到原来的单房间模式。

## 前置条件

- 一个给桥用的 **Matrix 账号**（也就是你在控制房间里输入时用的账号），且已加入控制房间。
- 它的**密码** —— `bridge.mxlogin` 用密码签发属于桥自己的 token。
  （Element ▸ *设置 ▸ 帮助与关于 ▸ 访问令牌* 里的 **access token** 也能用，走 `--token`，
  但那个 Element 会话一登出 token 就失效。）
- 控制房间的**内部 id** —— Element ▸ *房间设置 ▸ 高级 ▸ 内部房间 ID*（`!…:server`）。
- Telegram 的 **api_id / api_hash**，来自 <https://my.telegram.org> ▸ *API development tools*。

## 配置

```bash
cp config.example.yaml config.yaml   # homeserver、user_id、control_room
cp .env.example .env                  # MATRIX_ACCESS_TOKEN、TELEGRAM_API_ID/HASH
```

环境变量覆盖文件（密钥推荐这样传）：`MATRIX_HOMESERVER`、`MATRIX_USER_ID`、
`MATRIX_CONTROL_ROOM`、`MATRIX_ACCESS_TOKEN`、`MATRIX_PASSWORD`、`TELEGRAM_API_ID`、
`TELEGRAM_API_HASH`、`TELEGRAM_PHONE`、`BRIDGE_PROXY`。

## 用 Docker 运行（推荐）

Telegram 登录是交互式的，用 `docker compose run` 做一次；session 会写进 `./tgdata`
（Matrix store 和 state 也在这里）。

```bash
docker compose build

# 1) 一次性 Telegram 登录 —— 输入 Telegram 发给你的验证码（以及两步验证密码）
docker compose run --rm --entrypoint python bridge \
    -m bridge.tglogin --config /config/config.yaml

# 2) Matrix 登录（随时可重做，见"更换 Matrix 账号"）
docker compose run --rm --entrypoint python bridge \
    -m bridge.mxlogin --config /config/config.yaml

# 3) 运行
docker compose up -d
docker compose logs -f      # 看到 "telegram authorised as ..." 和 "matrix sync starting" 就成了
```

## 命令行工具

两个命令行工具，都是交互式的，都要在**桥所在的机器上**运行：

| 工具 | 用途 | 频率 |
|------|------|------|
| `bridge.tglogin` | 登录 Telegram 用户账号（验证码 + 两步验证），生成 `telegram.session` | 一次 |
| `bridge.mxlogin` | 设置或更换 Matrix 账号，生成 `matrix_creds.json` | 随时 |

### 怎么启动

```bash
# Docker（常规情况）—— 在 docker-compose.yml 所在目录执行
docker compose run --rm --entrypoint python bridge -m bridge.mxlogin --config /config/config.yaml
docker compose run --rm --entrypoint python bridge -m bridge.tglogin --config /config/config.yaml

# 不用 Docker
python -m bridge.mxlogin --config config.yaml
```

桥在**远程服务器**上时，用封装脚本而不要手动 SSH——它让登录动作在服务器上执行，
于是 homeserver 记录到的是服务器地址而不是你本机的，密码也只在 SSH 隧道内传输：

```bash
./scripts/mxlogin.sh root@vps.example.com            # POSIX shell
.\scripts\mxlogin.ps1 -Server root@vps.example.com   # PowerShell
```

把这两个变量设一次，以后裸跑脚本就行：

```powershell
[Environment]::SetEnvironmentVariable("BRIDGE_SSH_HOST",    "root@vps.example.com", "User")
[Environment]::SetEnvironmentVariable("BRIDGE_REMOTE_PATH", "/srv/matrix-telegram-bridge", "User")
```

```bash
export BRIDGE_SSH_HOST=root@vps.example.com
export BRIDGE_REMOTE_PATH=/srv/matrix-telegram-bridge
```

### `bridge.mxlogin` 参数

| 参数 | 作用 |
|------|------|
| `--config PATH` | 配置文件（默认取 `$BRIDGE_CONFIG`，否则 `config.yaml`） |
| `--homeserver URL` | 跳过 homeserver 提问 |
| `--user @name:server` | 跳过 user id 提问（token 模式下忽略，以 `/whoami` 为准） |
| `--room !id:server` | 跳过控制房间提问；填 `#别名:server` 会自动解析 |
| `--device-name NAME` | 在 homeserver 上显示的设备名（默认 `MATRIX_TG_BRIDGE`） |
| `--token` | 用已有的 access token 而不是密码（SSO 账号） |
| `--token-stdin` | 从 stdin 读那个 token —— 隐含 `--token` |
| `--password-stdin` | 从 stdin 读密码而不是提问 |
| `--no-egress-check` | 跳过出口 IP 查询（少一次第三方请求） |
| `-y`, `--yes` | 所有确认都当作 yes |
| `-h`, `--help` | 用法 |

退出码：`0` 成功 · `1` 在提问处主动取消 · `2` 代理不可用 · `3` 认证被拒 ·
`4` 控制房间进不去。

`bridge.tglogin` 只有 `--config`，其余（手机号、验证码、两步验证密码）都是问你的。

### 跑起来长这样

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
  homeserver [https://matrix.org]:        <- 回车表示接受方括号里的默认值

  how do you want to authenticate?
    1) password  - mints a NEW token owned by the bridge (recommended)
    2) token     - paste an existing access token (SSO accounts)
  choice [1]:

[ login ]-----------------------------------------------------
  password (not echoed):                  <- 不回显
  [ok]   new device: ABCD1234 ('MATRIX_TG_BRIDGE')
  [ok]   authenticated as @you:matrix.org

[ control room ]----------------------------------------------
  [ok]   control room joined: !yourRoom:matrix.org

[ store ]-----------------------------------------------------
  [ok]   credentials written to /data/matrix_creds.json
```

那行出口地址是安全检查，不是装饰：**在输密码之前**确认它就是你预期的地址。
如果该显示 VPN 出口却显示了你真实的 ISP 地址，就回答 `n` 中止。

### 两个会踩的坑

**别在 Git Bash / MSYS 里跑。** 它们会把 `/config/config.yaml` 改写成
`C:/Program Files/Git/config/config.yaml`。用 PowerShell，或者加前缀：

```bash
MSYS_NO_PATHCONV=1 ./scripts/mxlogin.sh
```

**`-T` 会关掉 TTY**，密码提示直接收到 EOF 然后退出。只有配合下面的脚本化参数
才应该加 `-T`。

### 脚本化（无交互）

密钥走 **stdin，绝不走 argv** —— argv 对其他进程可见，而且会进 shell 历史。
管道里的那一行会在任何提问之前被取走，所以不会被误当成某个问题的答案：

```bash
printf '%s\n' "$TOKEN" | docker compose run --rm -T --entrypoint python bridge \
    -m bridge.mxlogin --config /config/config.yaml \
    --token-stdin --room '!yourRoom:matrix.org' -y
```

有默认值的问题在没有 tty 时会自己用默认值；没有默认值的必须用参数传。

## 更换 Matrix 账号

`bridge.mxlogin` 交互式认证后把结果写进 `/data/matrix_creds.json`，这个文件会
**覆盖** `config.yaml` 和 `.env` 里的 homeserver / user id / token / 控制房间。
两种认证方式：

| | 做法 | 什么时候用 |
|---|---|---|
| **密码**（默认） | 登录并签发一个属于**桥自己**设备的 token | 常规情况——Element 登出也不受影响 |
| **token**（`--token`） | 采用你粘贴的 access token，先用 `/whoami` 验证 | 没有密码的 SSO 账号 |

token 这条路的 user id 和 device id 是从 `/whoami` 反查的，不是信你输入的：
token 和它声称的账号对不上的话，否则要到 sync 阶段才暴露，表现是"莫名其妙收不到消息"。

**不需要重启。** 运行中的桥每 2 秒对 `matrix_creds.json` 做一次哈希；一旦变化就
拆掉现有接线、按新账号重建，大约一秒完成。触发器是文件本身——这正是它能生效的原因，
因为 `mxlogin` 跑在**另一个容器**里，两者共享 `/data` 挂载。

如果新配置加载失败，桥会记录错误并**继续用旧账号运行**，不会因此停摆。

这样一劳永逸解决了两件事：

* 凭据在数据卷上而不在 `.env` 里，改动只需要 `restart`，不用 `up --force-recreate`。
* token 属于桥自己的设备，登出 Element 会话不会再让桥挂在 `M_UNKNOWN_TOKEN` 上。

### 换控制端账号

只能用 `bridge.mxlogin`（服务器上跑）。Matrix 这边**固定一个账号**做控制端 —— 多账号
是 Telegram 那一侧的事，见下一节。

切换到**不同的 user id** 时会销毁上一个 Matrix 账号的缓存：`msglinks.json`（转发正文）、
`rooms.json`（房间映射）、`outbox.json`（待发队列）、nio store（同步位置和设备密钥）、
`state.json` 里的房间→目标映射，以及进程被杀留下的 `<文件>.tmp`（那里面是完整副本）。
Telegram 账户、会话、静音/白名单等一概保留 —— TG 那侧并没有变。

## 多个 Telegram 账户

**Matrix 一个号做控制端，Telegram 可以同时登录多个号。**每个 TG 账户：

- 绑定**一个 Matrix 空间（Space）**，它的专属房间都建在那个空间里；
- 拥有自己的会话文件和缓存（`accounts/tg-<id>/`），彼此看不见；
- **同时在线** —— 不是切换着用，所有账户的消息都在各自空间里实时转发。

```
!tg accounts                     列出账户：名字、id、绑定的空间，⭐标出当前
!tg login +8613800138000         登录新账户（下面详述）
!tg switch 2                     切换"当前"账户
!tg bind / unbind                绑定/解绑空间
!tg logout <账户> confirm         退出
```

「当前账户」只决定**全局房间里**的命令和发送归谁 —— 专属房间里打字永远走那个房间
所属的账户，跟当前是谁无关。

### 登录（在空间里登录，自动绑定）

先在 Element 里建好空间并把桥账号拉进去，然后**在那个空间下的任意房间**里发：

```
!tg login +8613800138000
!tg code 12345
!tg 2fa <两步验证密码>        （只有开了两步验证才需要）
```

登录成功后这个空间就自动绑给该账户了 —— 不用复制任何房间 id。也可以显式指定
`space=<空间id>`，或先登录、之后再在目标空间的房间里发 `!tg bind`。

账户相关命令（accounts / login / code / 2fa / switch / bind / logout）在**桥所在的
任何房间**都能用，正是为了让你能在目标空间里登录；其他命令仍然只在全局房间生效。

> ⚠️ **验证码和两步验证密码会进入房间历史。** 桥会立即撤回（redact）那条命令消息，
> 但和 Matrix 密码那节说的一样：服务器已经收到过，redact 只是协议层面的内容清除。
> 想完全避免就在服务器上登录：
> `docker compose run --rm --entrypoint python bridge -m bridge.tglogin --config /config/config.yaml --space '!空间id:matrix.org'`
> 两条路进的是同一个账户列表，`!tg accounts` 都能看到。

### switch 和 logout 的区别

| | 做什么 | 破坏性 |
|---|---|---|
| `switch` | 换"当前"账户 | **零**；所有账户仍然在线，各自的专属房照常收发 |
| `logout` | TG 端注销该会话 + 删本地会话文件和缓存 + 移出列表 | **有**，需要 `confirm` |

`logout` 会真的在 Telegram 端注销那个会话 —— 只删本地会话文件的话，账户上会永远
挂着一个已授权设备。

### 数据布局

```
/data/
  matrix_creds.json          Matrix 控制端账号（热重载触发器）
  telegram_accounts.json     TG 账户列表（600 权限）
  state.json                 全局设置：静音、白名单、延迟、自毁、前缀
  store/                     Matrix 同步库
  accounts/
    tg-1234567890/           telegram.session · rooms.json · msglinks.json
    tg-7788990011/           outbox.json · expire.json  ← 互不可见
```

账户之间**不共享任何缓存**，所以一个账户的转发正文和房间映射不可能被另一个读到 ——
这是结构上保证的，不靠"记得清理"。

从单账户版本升级时，原来的 `telegram.session` 会被自动收编成账户 #1，
`matrix.space` 变成它绑定的空间，已有的房间映射和消息链接原样搬进它的目录（日志里
会写明搬了什么）。不用重新登录，也不会重复建房。

#### 清不到的地方

即便是 logout，这也只是本地卫生，不是"每一份副本都被销毁"的保证：

* **已经转发进 Matrix 房间的消息仍在 homeserver 上**（以及收到过它们的联邦服务器上）。
  真要清干净，在 Element 里删除或退出那些房间/空间。
* 已同步到其他客户端的内容、推送通知记录、服务器备份、联邦副本 —— 桥都够不着。
* redact 清除的是事件正文，不是"这个事件存在过"这件事。


## 代理与隐私

一个代理同时覆盖两侧 —— Matrix（通过 `aiohttp-socks` connector）和
Telegram（通过 `python-socks`）：

```yaml
proxy:
  url: "system"    # 默认：跟随本机代理设置
  # url: "socks5h://127.0.0.1:1080"   # 显式指定（或用 .env 里的 BRIDGE_PROXY）
  # url: "none"                        # 明确直连
```

`system`（默认值，留空也是这个意思）依次读 `ALL_PROXY` / `HTTPS_PROXY` /
`HTTP_PROXY`，Windows 上再查 WinINET 注册表设置。启动日志会写明实际走的哪条路：

```
egress: via socks5h://127.0.0.1:1080 (from system)
egress: DIRECT - no system proxy found (expected behind a full-tunnel VPN; ...)
```

### 配合全局 VPN（WireGuard / Mullvad）

全局 VPN **不是**代理 —— 它在网络层接管流量，根本不设置系统代理。所以 `system`
什么都找不到是正确的，`DIRECT` 在这里的真实含义就是"走隧道"。代理留空即可。

真正值得验证的是 Docker 的 NAT 会不会逃出隧道。别猜，从容器内部实测：

```bash
docker compose run --rm --entrypoint python bridge -c \
  "import json,urllib.request;print(json.load(urllib.request.urlopen('https://am.i.mullvad.net/json')))"
# mullvad_exit_ip: True  -> 容器流量在隧道内
```

Mullvad 还提供一个**只能从 WireGuard 隧道内访问**的 SOCKS5 代理
（`socks5h://10.64.0.1:1080`），如果你想显式钉死出口而不依赖默认路由，可以用它。

* 优先用 `socks5h://` 而不是 `socks5://` —— 那个 `h` 表示 **DNS 在代理端解析**，
  你的 resolver 就看不到目标域名。用了本地 DNS 那种形式桥会告警。
* **失败即关闭**：配了代理但用不了（URL 错误、传输库缺失），桥和 CLI 都会退出，
  绝不静默改成直连。
* MTProto 走不了 HTTP 代理 —— Telegram 必须用 SOCKS。
* `telegram.device_model` / `system_version` / `app_version` 在配置里钉死，
  这样 Telethon 上报的是通用客户端信息，而不是本机真实的 `uname`。
* `mxlogin` 会在你输密码**之前**显示远端将记录到的出口地址
  （`--no-egress-check` 可跳过这次查询）。

## 本地运行（不用 Docker）

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows；*nix 用 source .venv/bin/activate
pip install -r requirements-dev.txt

python -m bridge.tglogin --config config.yaml   # 一次，交互式
python -m bridge.mxlogin --config config.yaml   # Matrix 账号，随时
python -m bridge --config config.yaml
```

*（本地运行时把 `store_path`、`session`、`state_path` 改成 `./store`、
`./telegram.session`、`./state.json` 这样的本地路径，而不是 `/data/...`。）*

## 跑测试

测试**不联网、不需要凭据、不需要 Telegram/Matrix 账号** —— 所有端口都用内存
假件驱动，所以刚 clone 下来、还没配置任何东西时就能直接跑。

```bash
# 用 Docker（什么都不用装；Dockerfile.test 是开发镜像）
docker build -f Dockerfile.test -t mtb-test .
docker run --rm -v "$PWD:/src" mtb-test python -m pytest -q

# 本地
pip install -r requirements-dev.txt
pytest -q
```

469 个测试，约 4 秒。常用变体：

```bash
pytest -q tests/test_relay.py          # 只跑一个文件
pytest -q -k forward                   # 按名字筛选
pytest -q -x -vv                       # 第一个失败就停，详细输出
```

Windows 上挂载需要绝对路径：PowerShell 用 `-v "${PWD}:/src"`，
Git Bash 用 `MSYS_NO_PATHCONV=1 docker run ... -v "/$(pwd):/src"`。

## 测试策略

整条控制／转发链路都通过端口用内存假件测试 —— **不联网，不碰 SDK**：

- `test_dispatcher.py` —— 命令解析 + 当前目标／`@` 临时目标的路由
- `test_relay.py` —— 打标签、静音→不提醒、媒体获取与降级
- `test_state.py` —— 当前目标和静音表的持久化
- `test_matrix_sink.py` —— msgtype、防环标记、媒体上传（假 nio）
- `test_telegram_user_sink.py` —— send_message/send_file 映射（假 Telethon）
- `test_proxy.py` / `test_system_proxy.py` —— 代理 URL 解析、系统代理探测、失败即关闭
- `test_creds.py` —— 凭据读写、优先级覆盖、损坏文件的降级
- `test_hot_reload.py` —— 凭据变更检测（内容哈希）
- `test_mxlogin.py` —— stdin 密钥处理、认证方式选择、无 tty 时的默认值
- `test_supervisor.py` —— 启动失败会写日志并以非零码退出；换号时在旧 App 完全
  停止后再清理一次
- `test_messagelinks.py` —— tg↔matrix 链接库：精确查找 vs 按 msg id 兜底、
  event id 反查索引、淘汰、关闭时落盘
- `test_expirer.py` —— 自毁到期时标记 Matrix 副本而不是真删
- `test_purge.py` —— 清理时清掉什么、必须保留什么
- `test_accounts.py` —— Telegram 账户列表、收编单账户安装时不能弄丢房间映射，
  以及重新登录只换 session、不动缓存
- `test_account_commands.py` —— `!tg accounts / login / code / 2fa / switch /
  bind / logout`：验证码被撤回且绝不回显、空间取自命令所在房间、
  离线账户仍然可管理
- `test_ordering_and_deleted.py`、`test_new_commands.py`、`test_rooms.py`、
  `test_dms.py`、`test_bots_and_avatars.py`、`test_matrix_rooms.py` ——
  专属房间路由、删除／编辑标记、建房、头像同步、机器人过滤
- `test_outbound_scheduler.py`、`test_replymap.py`、`test_duration.py`、
  `test_telegram_user_source.py` —— 发送延迟／定时与重试、回复映射、
  时长解析、读取 Telethon 的回复/转发头
- `test_transformer.py`、`test_config.py` —— 纯函数与配置校验

## Fork / 发布

所有能识别到你的东西在设计上就不进仓库 —— `.gitignore` 排除了 `.env`、
`config.yaml`、`tgdata/`、`store/` 和 `*.session*`。被追踪的只有 `.example`
文件，里面全是占位符。

推送 fork 之前自己确认一遍：

```bash
git status --ignored --short          # 你的真实文件应当出现在 ignored 里
git ls-files | grep -Ei 'env|session|config\.yaml'   # 预期只剩 .example
```

三件值得知道的事：

- **绝不要提交 `config.yaml`。** 里面有你的 Matrix user id、控制房间和空间 id，
  就算没有 token 也足以定位到账号。
- **`tgdata/` 是最敏感的。** `msglinks.json` 存的是转发消息的**正文**，
  `rooms.json` 是 Telegram 对话 id 到 Matrix 房间的映射。别进仓库，也别放进
  任何要分享的备份里。
- **凡是提交过的都要换掉。** token 或 `api_hash` 一旦进过公开提交，删掉之后
  仍然留在历史里 —— 去 <https://my.telegram.org> / Element 重新生成一个。

## 说明与限制

- 面向**未加密**的 Matrix 房间。E2EE 需要 `libolm` 和附件解密，为了镜像精简没有做。
- 控制房间应当**只有你自己**：桥会响应房主账号的消息，并把你全部 Telegram 流量显示在里面。
- 启动时忽略 Matrix 历史消息（只有新消息才会被当成命令执行）。
- 每条消息都是尽力投递；失败只记日志，不会让进程退出。
- 用用户账号做自动化受 Telegram 服务条款约束 —— 保持正常、非滥用的发送节奏。
