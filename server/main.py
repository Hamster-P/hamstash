import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config_store
from db_migrate import consume_family_cache_rewarm_flag, upgrade_db
from routers import backup, detail, download_submit, download_tasks, library, media, rss_engine, search, settings, setup, tracking
from services.proxy import init_proxy_url_cache
from services.organize import organize_loop
from services.rss_poller import rss_poll_loop
from services.library_health import library_root_health_loop

config_store.DEFAULTS["library_root"] = r"D:\AnimeLibrary"  # 找不到用户配置时的默认值

try:
    # library_root此刻可能不可达(网络共享/移动硬盘开机时经常比这个服务本身
    # 启动得慢)——建表这一步失败不能让整个后端进程直接崩掉退出,不然前端
    # 连"当前媒体库路径无法访问"这条提醒都没法显示(提醒本身要靠后端接口)。
    # 跳过建表不影响后续:library_root_health_loop检测到library_root变得
    # 可达时会自动补跑一次upgrade_db()。
    upgrade_db()
except Exception as e:
    print(f"[DB] 启动时建表失败,library_root可能暂时不可达,等它恢复后会自动重试: {e}")

async def rewarm_family_cache_task():
    """季度算法升级导致家族缓存被清空之后,把媒体库里已绑定bgm_id的番重新预热一遍。

    不预热的话,缓存空着的这段时间里媒体库详情页拿不到"作品名目录"的合法桶名,
    那些目录会暂时折叠进Specials/Others,要等用户下次点"修复媒体库"才恢复。
    这纯粹是升级带来的临时退化,不该让用户自己去点。

    只在真的清过缓存的那一次启动跑(consume_family_cache_rewarm_flag),
    平常启动完全不做这件事。逐个串行、复用现成的prefetch_rename_cache_task
    (它自己开session、自己吞异常、自带同bgm_id去重),失败不影响别的番——
    没预热上的家族在真正被用到时会自然按未命中重算。
    """
    import re

    from database import SessionLocal
    from models import LocalMedia
    from services.bgm_series_cache import prefetch_rename_cache_task

    db = SessionLocal()
    try:
        targets = []
        for m in db.query(LocalMedia).filter(LocalMedia.bgm_id.isnot(None)).all():
            # 传进去的是"番名",不是文件夹名——它会被当成resolve_series_identity的
            # fallback_title,带着" [bgm-N]"后缀的话可能拼出重复后缀的标题。
            title = re.sub(rf"\s*\[bgm-{m.bgm_id}\]$", "", m.folder_name).strip()
            targets.append((m.bgm_id, title or m.folder_name))
    finally:
        db.close()

    print(f"[REWARM] 家族缓存已重置,开始重新预热 {len(targets)} 部番")
    for bgm_id, folder_name in targets:
        await prefetch_rename_cache_task(bgm_id, folder_name)
    print("[REWARM] 预热完成")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_proxy_url_cache()  # 填充内存缓存,之后get_proxy_url()不再查DB(见services/common.py)
    task = asyncio.create_task(organize_loop())
    rss_task = asyncio.create_task(rss_poll_loop())
    health_task = asyncio.create_task(library_root_health_loop())
    rewarm_task = asyncio.create_task(rewarm_family_cache_task()) \
        if consume_family_cache_rewarm_flag() else None
    yield
    task.cancel()
    rss_task.cancel()
    health_task.cancel()
    if rewarm_task:
        rewarm_task.cancel()


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
app.include_router(download_submit.router)  # DownloadPage 下载
app.include_router(download_tasks.router)   # DownloadManagerPage 下载详情
app.include_router(rss_engine.router)  # RssPage    RSS订阅一览(RSS引擎,见services/rss_poller.py)
app.include_router(settings.router)  # SettingsPage 设置
app.include_router(setup.router)     # QbittorrentSetupPage 首次引导
app.include_router(backup.router)    # SettingsPage 备份与迁移
app.include_router(media.router)     # 通用图片代理,跨页面复用

