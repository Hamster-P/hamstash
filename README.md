# HamStash（囤番鼠）

![version](https://img.shields.io/badge/version-0.5.1-orange)
![platform](https://img.shields.io/badge/platform-Windows-blue)
![license](https://img.shields.io/badge/license-GPL--3.0-green)

个人使用的本地动漫媒体库管理工具。以 Bangumi 为数据源管理追番进度，把「订阅 → 资源匹配 → 整理归档 → 观看记录」这条链路自动化，让本地番剧库始终保持整齐、可续看。资源的实际获取通过你本机的 qBittorrent 完成。

<!-- TODO: 补充截图 —— 建议放追更日历、下载页、本地媒体库三张 -->

## 这是什么 / 不是什么

HamStash 解决的是「追一堆番、手动整理分类、手动记录看到哪、手动开播放器」这一整套体力活。

它不是资源站，也不是下载器，本身不托管、不提供、不分发任何影视资源。番剧的检索结果来自 dmhy / AnimeGarden / nyaa.si 等独立的第三方公开站点，实际下载由你本机的 qBittorrent 执行——HamStash 只是把这些你原本要手动完成的步骤串起来、自动化掉的中间调度层。你连接哪些站点、下载哪些内容，均由你自行决定并自行负责。

因为是按自己的使用习惯做的个人工具，界面和数据源目前只覆盖日语番剧、只做了 Windows 桌面端的完整体验（含开机自启的 Windows 服务）。

## 功能

按"从追更到看上"的使用流程排列：

- **追更**：本季新番播出日历，按星期几分组展示，自动过滤掉非日本制作的条目；点进去能看到简介、封面、放送日期
- **搜索与下载**：关键词/年份/季度检索 Bangumi 番剧库；对接 dmhy / AnimeGarden / nyaa.si 等第三方公开站点做资源匹配，支持画质/字幕语言/文件格式筛选，提交下载前有改名预览可以确认目标文件名，确认后交给你本机的 qBittorrent 执行
- **RSS 订阅自动追更**：给某部番建一条订阅规则（关键词 + 字幕组 + 画质），后台按设定的间隔自动轮询、命中就自动提交下载——这是完全自建的轮询引擎，不依赖 qBittorrent 自带的 RSS 功能（原生方案在处理 nyaa 这类需要二次请求 .torrent 文件的源、以及规则命名冲突时都不够可靠）
- **下载完成自动整理归档**：文件到位后自动按「番名/季度/集数」改名并归档进媒体库目录；也可以关闭自动改名，原样保留目录结构（应对 BD 原盘/合集光盘这类不该被拆的资源）
- **本地媒体库**：展示已入库番剧的封面、中文标题、简介，记录每一集的观看进度；支持内置的 mpv 连续播放（自动接播下一集），也可以配置呼出外部播放器（如 PotPlayer，会监视播放器窗口标题来同步观看进度）
- **手动匹配**：文件夹名字和 Bangumi 条目对不上时，可以手动搜索绑定，不需要改动物理文件夹本身
- **首次引导**：第一次启动会检测本机 qBittorrent 的安装/WebUI 状态，分三种情况引导配置，还能一键应用推荐的 qBittorrent WebUI 设置（开启 RSS 处理、关闭 Host Header 校验、放宽防爆破锁定阈值）
- **备份与迁移**：一键导出/导入数据库，方便换机

更详细的分页面操作说明见 [docs/MANUAL.md](docs/MANUAL.md)。

## 技术架构

- **客户端**（`client/`）：Tauri 2 + React 19 + TypeScript + Tailwind CSS v4；内置 mpv 作为 Tauri sidecar 播放器，通过命名管道做播放列表/进度 IPC；Rust 侧用 Win32 API 监视 PotPlayer 窗口标题以支持外部播放器进度同步
- **后端**（`server/`）：Python + FastAPI + SQLite（SQLAlchemy），对接 qBittorrent WebUI API 和 Bangumi API，`anitopy` 做文件名解析，`beautifulsoup4` 解析 dmhy/nyaa 页面
- **路由约定**：`server/routers/` 下每个文件与 `client/src/pages/` 下的一个页面一一对应，`services/` 里放不依附于单个页面的后台逻辑（RSS 轮询、下载完成后的整理归档等）
- 打包为 Windows 桌面应用：后端用 PyInstaller 打包成独立 exe，通过 NSSM 注册为本地 Windows 服务（`HamStashServer`），随系统自启

## 依赖条件

- 一份正在运行、已开启 WebUI 的 [qBittorrent](https://www.qbittorrent.org/)（首次启动会引导填写连接信息）
- Windows（NSIS 安装包 / Windows 服务部署方式目前只支持 Windows；也提供 `docker-compose.yml` 供 Linux/容器环境跑后端，但媒体库/播放器这些桌面端功能不适用）

## 下载安装

前往 [Releases](https://github.com/Hamster-P/hamstash/releases) 下载最新的 `HamStash_x.y.z_x64-setup.exe`（NSIS 安装包）或 `.msi`，双击安装即可，安装程序会自动注册并启动后台服务。卸载时数据库/设置不会被删除，重装或升级不影响已有数据。

## 开发

```bash
# 前端
cd client
npm install
npm run tauri dev

# 后端
cd server
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## 打包发布

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1
```

会依次：用 PyInstaller 打包后端 → 下载/准备 NSSM → 用 Tauri 打出 NSIS/MSI 安装包，产物在 `client\src-tauri\target\release\bundle\` 下。

## 数据存储

数据库（观看记录、订阅规则、下载任务等）默认在系统未配置媒体库目录前存于 `%ProgramData%\hamstash\`；一旦在设置页配置了媒体库目录，会自动迁移到该目录下的隐藏文件 `.anime_hub.db`，保证换电脑/重装系统时只要媒体库所在盘还在，数据就跟着找得回来。

## 免责声明
- 本软件为个人开发的媒体库管理工具，仅供个人学习、研究与交流使用，任何人不得将其用于商业用途，亦不得用于任何违法违规活动。
- 本软件自身不托管、不提供、不分发任何影视资源。软件所对接的 dmhy、AnimeGarden、nyaa.si 等为独立的第三方公开站点，其内容与本项目无关，本项目对这些站点的可用性、合法性及内容不作任何担保、亦不承担任何责任。
- 用户通过本软件检索、下载、存储的一切内容，以及由此产生的一切后果，均由用户本人自行判断并承担全部责任，与本软件作者无关。请在使用前确认相关行为符合你所在国家或地区的法律法规，并尊重相关版权。
- 本软件代码基于 GPL-3.0 开源。任何人对代码进行修改、人为去除相关限制后再行分发、传播并造成责任事件的，由修改发布者自行承担全部责任。
- 若本软件无意中侵犯了您的合法权益，请通过 Issue 联系，我会及时处理。

## License

[GPL-3.0](LICENSE) —— 可以自由使用/修改/分发，但衍生项目也必须保持开源。
