import asyncio
from contextlib import asynccontextmanager
from database import Base, engine
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config_store
from db_migrate import upgrade_db
from routers import backup, detail, download, downloads, library, rss, search, settings, setup, tracking
from services.organize import organize_loop

config_store.DEFAULTS["library_root"] = r"D:\AnimeLibrary"  # 找不到用户配置时的默认值

upgrade_db()
Base.metadata.create_all(bind=engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(organize_loop())
    yield
    task.cancel()


app = FastAPI(title="Anime Hub API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Hello, Anime Hub!"}


# 每个路由文件对应前端的一个画面,方便按页面定位/维护对应的接口
app.include_router(library.router)   # LibraryPage  影视库
app.include_router(search.router)    # SearchPage   搜索
app.include_router(detail.router)    # DetailPage   详情
app.include_router(tracking.router)  # TrackingPage 追更
app.include_router(download.router)  # DownloadPage 下载
app.include_router(downloads.router) # DownloadManagerPage 下载详情
app.include_router(rss.router)       # RssPage      RSS订阅一览
app.include_router(settings.router)  # SettingsPage 设置
app.include_router(setup.router)     # QbittorrentSetupPage 首次引导
app.include_router(backup.router)    # SettingsPage 备份与迁移

