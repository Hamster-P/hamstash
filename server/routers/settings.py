"""对应前端 SettingsPage(设置):下载/媒体库路径、轮询间隔、qBittorrent连接检测。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
from database import get_db
from models import AppSetting
from schemas import SettingsUpdate
from services.common import get_setting

router = APIRouter(tags=["设置"])


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return {
        "download_root": get_setting(db, "download_root", config_store.DEFAULTS["download_root"]),
        "library_root": get_setting(db, "library_root", config_store.DEFAULTS["library_root"]),
        "potplayer_path": get_setting(db, "potplayer_path", config_store.DEFAULTS["potplayer_path"]),
        "player_mode": get_setting(db, "player_mode", config_store.DEFAULTS["player_mode"]),
        "default_source": get_setting(db, "default_source", config_store.DEFAULTS["default_source"]),
        "rename_poll_interval_seconds": int(
            get_setting(
                db,
                "rename_poll_interval_seconds",
                config_store.DEFAULTS["rename_poll_interval_seconds"],
            )
        ),
        "qbit_host": get_setting(db, "qbit_host", config_store.DEFAULTS["qbit_host"]),
        "qbit_port": int(get_setting(db, "qbit_port", config_store.DEFAULTS["qbit_port"])),
        "qbit_username": get_setting(db, "qbit_username", config_store.DEFAULTS["qbit_username"]),
        # qbit_password 不回传:设置页/引导向导只负责写入,不需要把已保存密码明文读回前端。
        "qbit_setup_completed": get_setting(
            db, "qbit_setup_completed", config_store.DEFAULTS["qbit_setup_completed"]
        ) == "true",
    }


@router.put("/settings")
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    old_library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])

    values = {
        "download_root": payload.download_root,
        "library_root": payload.library_root,
        "potplayer_path": payload.potplayer_path,
        "player_mode": payload.player_mode,
        "rename_poll_interval_seconds": str(payload.rename_poll_interval_seconds),
        "default_source": payload.default_source,
    }
    for key, value in values.items():
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
        else:
            row = AppSetting(key=key, value=value)
            db.add(row)
    db.commit()
    config_store.write_ini(values)  # 双写本地INI,避免数据库丢失/迁移时设定跟着丢

    # 库目录变了:数据库要挪到新目录下,但这是进程启动时才做的引导逻辑(见database.py),
    # 不在这里做运行中途的热搬运,提示前端需要重启应用才会真正生效。
    restart_required = payload.library_root != old_library_root
    return {**values, "restart_required": restart_required}


@router.get("/qbittorrent/status")
async def qbittorrent_status():
    """健康检查:确认后端能不能正常登录qBittorrent。"""
    ok = await qbittorrent_client.test_connection()
    return {"connected": ok}
