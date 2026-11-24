# TiebaAgent

可独立部署的贴吧阅读、回复和帖子搬运助手，带中文本地控制台。项目不依赖其他聊天机器人、私人电脑路径或浏览器扩展。账号、API 密钥、语料、知识库和运行记录均由使用者在本机建立。

## 功能

| 功能 | 控制台可设置的内容 |
|---|---|
| 阅读与回复 | 活动吧列表、扫描间隔、跟踪帖数、自动开关、审核模式、每日/每小时上限、最小间隔、每轮条数、随机延迟、沉默概率 |
| 模型 | 多套 OpenAI 兼容 API 配置；回复、视觉识图、搬运评分分别选择；接口地址、模型和密钥 |
| 源吧索引与搬运 | 指定源吧或轮转索引列表、每轮扫描吧数、目标吧、轮换目标、热度门槛、评分门槛、图片与正文上限、楼主续贴、来源链接 |
| 调度 | 回复与搬运各自的频率、每日上限和活跃时段；时区；跨午夜时段 |
| 账号 | 自己填写 BDUSS，或用项目专用浏览器登录后导入；支持签到配置 |
| 管理 | 草稿审核/丢弃、人格编辑、知识库更新、已发帖浏览/回复/点赞统计、全量排序与分页、自动刷新 |

打开主控制台后点击 **完整设置 / API / 登录**。设置页覆盖所有常用运行参数；配置也保存在本机 `config.json`。

搬运台有单独的 **启动搬运 / 停止搬运** 按钮。停止会关闭自动运行并清除待执行的立即搬运任务；发主题、快捷发布及补楼层前都会重查开关。平台已经接收的请求无法撤回，因此停止时仍可能需要等待最后一次发布回执。停止搬运不影响回复、签到、统计和控制台。

## 安装

使用 Python **3.11–3.14**，建议 Python 3.12。首次安装需要联网下载 Python 依赖和 Chromium。

### Windows

安装 Python 并加入 PATH 后，双击 `install.bat`；安装成功后双击 `start.bat`。或者在项目目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe console/server.py
```

### Linux / macOS

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python console/server.py
```

Linux 若缺少浏览器系统依赖，按照 [Playwright 浏览器安装文档](https://playwright.dev/python/docs/browsers) 安装，例如 `python -m playwright install --with-deps chromium`。这可能需要系统管理员权限。

启动终端会输出带随机 token 的本地控制台地址。默认只监听 `127.0.0.1:3190`。不要直接把控制台或浏览器服务暴露到公网；服务器远程访问可以使用 SSH 端口转发。

## 首次配置

1. 在完整设置页保存一套自己的 API：配置 ID、接口地址、模型名称及密钥。接口应兼容 Chat Completions；支持根地址、`/v1`、`/v1beta/openai` 等地址，不会重复拼接 `/v1`。
2. 在“回复 API”选择刚保存的配置。看图和搬运评分可以选择不同 API；开启看图时需要支持图片的模型。DeepSeek 的 `thinking=disabled` 参数默认不发，使用者确认接口支持后再开启。
3. 配置回复活动吧、搬运源吧、目标吧和调度。搬运的 `forums` 是**源吧**，`target` 才是**目标吧**。`sitewide` 模式只轮转你填写的 `sourceForums`，不会擅自扫描整个贴吧。
4. 点击“启动专用浏览器 / 登录”，本人完成登录，再点击“从专用浏览器导入登录态”；或保存自己的 BDUSS。项目使用独立浏览器资料目录，不导入日常浏览器的隐私数据。
5. 先保留审核模式，点击主控制台的生成草稿按钮检查效果。准备好后自行开启自动回复、搬运或签到。新安装默认全部关闭。

无桌面的服务器可配置 BDUSS 并启用无头浏览器。百度要求扫码、验证码或其他本人验证时，需要本人处理；程序不会绕过验证。平台接口、网页结构、账户限制以及模型可用性可能影响实际发布结果，控制台会保留失败记录。

浏览器渠道留空时使用 Playwright 下载的 Chromium。已经安装 Chrome 或 Edge 的电脑，也可以在设置中把渠道改为 `chrome` 或 `msedge`，使用独立的新资料目录启动该浏览器；仍不会读取日常浏览器的登录态。

## 密钥、数据与可迁移性

- API 密钥写入本机 `state/api-profiles.json`，BDUSS 写入 `state/auth.json`；接口不回显这两类密钥。
- 也可以在启动前设置 `TIEBA_AGENT_API_KEY` 环境变量，使用默认模型配置。`.env.example` 是说明模板，程序不会自动执行 `.env`。
- `state/`、`config.json`、浏览器登录资料、抓取语料、知识库和日志均被 Git 忽略；仓库不携带作者的私人数据或运行历史。
- 迁移时先安装依赖；由运行者私下转移自己的 `config.json`、`state/`、`data/`、`knowledge/` 与人格文件。浏览器登录态在不同系统间不保证通用，必要时重新登录。
- 人格只决定表达风格；发送权限、频率和时段由程序控制。默认人格不冒用真实人物身份。

## 开发与验证

```sh
python -m compileall -q src console tools tests
python -m unittest discover -s tests -v
node --test tests/repost-ui.test.mjs
python scripts/audit_release.py
```

浏览器测试使用本地合成 HTML 和合成图片，不登录贴吧、不发帖。安装 Chromium 后设置 `TIEBA_BROWSER_TEST=1` 再运行测试，可验证无系统剪贴板的图片上传和文本填写。

```powershell
$env:TIEBA_BROWSER_TEST="1"
python -m unittest discover -s tests -v
```

仓库的 CI 包含 Windows/Linux 检查。真实发帖需要运行者自己的账号和明确操作；自动测试不对外发布帖子。

## 目录

`src/` 为回复、搬运、浏览器和统计逻辑；`console/` 为控制台；`tools/` 为本地语料/知识库工具；`tests/` 为隔离测试。完整默认设置见 `config.example.json`。

依赖项目包括 [aiotieba](https://github.com/Starry-OvO/aiotieba)、[Playwright](https://github.com/microsoft/playwright-python) 和 [Pillow](https://github.com/python-pillow/Pillow)，各自遵循其许可证。
