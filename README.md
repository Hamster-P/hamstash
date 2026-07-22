# HamStash（囤番鼠）

个人使用的动漫追更 / 下载管理 / 本地媒体库桌面工具。基于 Bangumi 数据源检索番剧，通过 qBittorrent 自动下载并整理归档，内置播放器直接在本地库里连续观看。

## 功能

- **追更**：从 Bangumi 检索番剧，按年份/季度筛选，查看详情、简介、封面
- **下载**：对接 qBittorrent，支持关键词搜索资源（AnimeGarden / dmhy）手动下载，也支持长期 RSS 订阅自动追更新集
- **自动整理归档**：下载完成后按番名/季度/集数自动改名归档进媒体库；也可以关闭自动改名，原样保留目录结构（应对 BD 原盘/合集光盘）
- **本地媒体库**：展示已入库的番剧封面/中文标题/简介，记录观看进度；支持内置 mpv 连续播放或呼出外部播放器（如 PotPlayer）
- **手动匹配**：文件夹名对不上 Bangumi 条目时，可以手动搜索绑定，不需要改动物理文件夹
- **备份与迁移**：一键导出/导入数据库，方便换机

## 技术架构

- **客户端**（`client/`）：Tauri 2 + React + TypeScript + Tailwind CSS v4，内置 mpv 作为 sidecar 播放器
- **后端**（`server/`）：Python + FastAPI + SQLite（SQLAlchemy），对接 qBittorrent WebUI API 和 Bangumi API
- 打包为 Windows 桌面应用，后端以 PyInstaller 打包成独立 exe，通过 NSSM 注册为本地 Windows 服务

## 依赖条件

- 一份正在运行、已开启 WebUI 的 [qBittorrent](https://www.qbittorrent.org/)（首次启动会引导填写连接信息）
- Windows（NSIS 安装包 / Windows 服务部署方式目前只支持 Windows；也提供 `docker-compose.yml` 供 Linux/容器环境跑后端）

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
