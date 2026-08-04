# HamStash（囤番鼠）

![version](https://img.shields.io/badge/version-0.5.1-orange)
![platform](https://img.shields.io/badge/platform-Windows-blue)
![license](https://img.shields.io/badge/license-GPL--3.0-green)

个人使用的动漫追更 / 下载管理 / 本地媒体库桌面工具。基于 Bangumi 数据源检索番剧，通过 qBittorrent 自动下载并整理归档，内置播放器直接在本地库里连续观看。

<!-- TODO: 补充截图 —— 建议放追更日历、下载页、本地媒体库三张 -->

## 这是什么 / 不是什么

HamStash 解决的是"追一堆番、手动找资源、手动改名分类、手动开播放器"这一整套体力活。它**不是**一个资源站或下载器本身——资源搜索靠 dmhy / AnimeGarden / nyaa.si，真正下载靠 qBittorrent，HamStash 是把"追更信息 → 资源匹配 → 下载 → 改名归档 → 观看"这条链路串起来、自动化掉的中间层。

因为是按自己的使用习惯做的个人工具，界面和数据源目前只覆盖日语番剧、只做了 Windows 桌面端的完整体验（含开机自启的 Windows 服务）。

## 功能

按"从追更到看上"的使用流程排列：

- **追更**：本季新番播出日历，按星期几分组展示，自动过滤掉非日本制作的条目；点进去能看到简介、封面、放送日期
- **搜索与下载**：关键词/年份/季度检索 Bangumi 番剧库；在下载页对接 dmhy / AnimeGarden / nyaa.si 三个数据源，支持画质/字幕语言/文件格式筛选，下载前有改名预览可以确认目标文件名，一键提交给 qBittorrent
- **RSS 订阅自动追更**：给某部番建一条订阅规则（关键词 + 字幕组 + 画质），后台按设定的间隔自动轮询、命中就自动下载——这是完全自建的轮询引擎，不依赖 qBittorrent 自带的 RSS 功能（原生方案在处理 nyaa 这类需要二次请求 .torrent 文件的源、以及规则命名冲突时都不够可靠）
- **下载完成自动整理归档**：文件下载完自动按"番名/季度/集数"改名并归档进媒体库目录；也可以关闭自动改名，原样保留目录结构（应对 BD 原盘/合集光盘这类不该被拆的资源）
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

## License

[GPL-3.0](LICENSE) —— 可以自由使用/修改/分发，但衍生项目也必须保持开源。
