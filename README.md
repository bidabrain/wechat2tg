# wechat2tg

在 Docker 里常驻运行 **Linux 桌面版微信**，实时采集微信消息并转发到 **Telegram**，通过 Web 面板配置和扫码登录。可直接拉取 CI 构建的镜像，用 `docker compose` 部署。

> 思路参考 [watgbridge](https://github.com/akshettrj/watgbridge) 与 [bidabrain/wa](https://github.com/bidabrain/wa)：真机做主设备，容器里的客户端做转发端。微信没有开放的关联设备协议，所以用原生 Linux 版微信 + 数据库解密来采集消息。

## 特点

- **无需安卓模拟器 / KVM**：Linux 版微信是原生 x86_64 程序，纯容器运行，普通 VPS 即可。
- **读消息**：解密微信本地 SQLCipher 数据库，实时读取文字/图片/表情/链接/群消息，发送者映射到昵称。
- **发消息**：Telegram → 微信，模拟界面操作回发。
- **Web 面板**：noVNC 扫码登录、查看画面、配置转发、看日志。
- **统一镜像**：一份 Docker 镜像包含微信 + 采集 + 转发 + 面板。

> ⚠️ 项目处于开发阶段（P1）。消息采集已验证可用；Telegram 桥接、媒体文件(.dat)解密、发送到指定会话的稳定定位仍在完善中。

## 架构

```
Docker 容器
├─ Xvfb + openbox + x11vnc   虚拟显示器（微信是 GUI 程序）
├─ 微信 Linux 版（常驻）        原生运行，扫码登录为“电脑”设备
├─ 采集模块                    解密数据库、解析消息、监听新消息
├─ 转发模块                    微信 ⇄ Telegram
└─ 管理面板 (FastAPI)          noVNC / 配置 / 日志，Web 端口 8080
```

## 部署

### 1. 准备

- 一台装了 Docker 和 Docker Compose 的机器（Linux 推荐；Windows/Mac 用 Docker Desktop 也可）。
- 一个正在使用的微信号（你的真手机是主设备，容器里的微信是“电脑”附属设备）。

### 2. 用 docker compose 启动

新建一个目录，放入 [`docker-compose.yml`](docker-compose.yml)，改好面板密码后：

```bash
docker compose up -d
```

首次会拉取镜像 `ghcr.io/bidabrain/wechat2tg:latest`（CI 自动构建）。

关键配置说明（见 compose 文件）：
- `cap_add: SYS_PTRACE` — **必须**，用于读取微信进程内存提取数据库密钥。
- `shm_size: 1g` — 微信需要较大的 `/dev/shm`。
- `./data:/data` — 持久化登录态、数据库、配置、日志。重启容器不丢登录。
- 端口 `8080` — 管理面板。

### 3. 扫码登录

1. 浏览器打开 `http://<服务器IP>:8080`，输入面板密码。
2. 在 noVNC 画面里看到微信二维码，用**手机微信**扫码登录。
3. 登录后手机可正常使用；容器里的微信作为“电脑”设备保持在线。

> 电脑登录位全平台（Win/Mac/Linux）共用一个，若你平时用 PC 微信会被挤下线；平板(Pad)是另一个登录位，不冲突。
> 容器重启后一般**无需重新扫码**，只需在手机上点一下“登录”确认。

## 配置 Telegram 转发

### 第一步：创建 Bot，拿到 Token

1. 在 Telegram 里搜索 **@BotFather**（官方，带蓝标），开始对话。
2. 发送 `/newbot`，按提示设置 bot 名称和用户名（用户名必须以 `bot` 结尾，如 `my_wx2tg_bot`）。
3. BotFather 会返回一串 **token**，格式是「一串数字」+ 冒号 + 「一串字母数字」
   （例如 `110000000` 冒号 `AAFxxxx…` 共约 46 个字符）。这就是 `TG_BOT_TOKEN`。
   妥善保存，**切勿提交到代码仓库或公开分享**。

### 第二步：拿到你的 User ID

1. 在 Telegram 里搜索 **@userinfobot**，给它发任意消息。
2. 它会回复你的 `Id`，是一串数字，如 `88888888`。这是**你个人账号的 user id**。
3. 如果只想把所有微信消息转发给你自己，`TG_CHAT_ID` 就填这个数字。

> 注意：bot 不能主动给陌生人发消息。请先在 Telegram 里找到你的 bot，点 **Start**（发一条 `/start`），bot 才能给你发消息。

### 第三步（推荐）：建一个群并拿到群 Chat ID

推荐把消息转发到一个 **Telegram 群**，配合下面的 **Topic 模式**按会话分栏管理。

1. 新建一个 Telegram 群，把你的 bot 拉进去。
2. 获取群的 `chat id`：在群里发一条消息，浏览器访问
   `https://api.telegram.org/bot<你的TOKEN>/getUpdates`
   （把 `<你的TOKEN>` 换成整串 token，不要带尖括号），在返回 JSON 里找
   `"chat":{"id":-100xxxxxxxxxx,...}`。
3. 群 `chat id` 是以 **`-100` 开头的负数**（例如 `-1004457133614`），填到 `TG_CHAT_ID`。
   **注意别漏掉负号**，否则会报 `Chat not found`。

> 也可以转发给你自己：`TG_CHAT_ID` 填你的 user id（@userinfobot 查），并先对 bot 发 `/start`。
> 但这样所有会话混在一个对话里，不如用群 + Topic 模式清晰。

### 第四步：开启 Topic 模式（强烈推荐）

开启后，**每个微信会话（联系人/群）会自动对应群里的一个 Topic（话题）**：消息按会话分栏，
你**直接在对应 topic 里打字就发回该微信会话**，不用「回复某条消息」。需要两个设置：

1. **开启群的 Topics**：进群资料 → 编辑 → 打开「**Topics / 话题**」开关（群会变成论坛模式）。
2. **给 bot 加「管理话题」权限**：群资料 → **Administrators / 管理员** → 选中你的 bot →
   打开「**Manage Topics / 管理话题**」→ 保存。

没开 Topics 也能用（所有消息发到群主对话、带上会话名前缀），只是不能按会话分栏和直接回复。

### 第五步：填入配置

两种方式任选：
- **面板配置**（推荐）：在 Web 面板的「Telegram」标签填 `Bot Token` 和 `Chat ID`，保存即生效。
- **环境变量**：在 `docker-compose.yml` 的 `environment` 里填 `TG_BOT_TOKEN` / `TG_CHAT_ID`，`docker compose up -d` 重启。

填好后给这个微信号发条消息：bridge 会为该会话自动建一个 topic，消息进到那个 topic 里。
在 topic 里直接打字即可回发到微信。topic 映射保存在 `/data/topics.json`，重启不丢。

## 开发 / 本地构建

```bash
docker compose build        # 本地构建（会从腾讯官方下载微信 deb，约 230MB）
docker compose up -d
```

CI（GitHub Actions）在推送到 `main` 或打 `v*` tag 时自动构建并推送镜像到 GHCR，见 [`.github/workflows/docker.yml`](.github/workflows/docker.yml)。

## 说明与限制

- **封号风险**：本项目只读数据库 + 读一次进程内存，不改微信、不注入、不碰协议，风险较低，但任何非官方用法都无法保证零风险，请用小号/自担风险。
- **发送定位**：微信搜索结果和会话列表会动态重排，“发送到指定会话”的稳定定位仍在完善（当前对“当前会话”的发送稳定）。
- **媒体文件**：图片/视频是加密的 `.dat`，解密转发仍在开发中。
- **掉线**：长时间不活跃或微信升级可能掉线，面板/Telegram 会提示，手机确认或重新扫码即可。

## 许可

仅供学习研究，使用者自行承担风险。
