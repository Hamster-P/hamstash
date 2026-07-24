"""对应前端 QbittorrentSetupPage(首次引导):测试/保存qBittorrent连接信息,应用推荐偏好设置。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import qbittorrent_client
from database import get_db
from schemas import QbitApplyRequest, QbitTestRequest
from services.common import upsert_setting

router = APIRouter(prefix="/setup", tags=["首次引导"])

# 应用后能让anime-hub跑得更顺的几项偏好设置:
# - RSS处理/自动下载:RSS订阅功能本身依赖的开关,不开这两个订阅规则不会生效
# - Host头校验关掉:非localhost/非标准方式访问WebUI时常见的401误报,自建场景没必要开
# - 防爆破锁定阈值放宽:避免正常使用时偶发触发qBittorrent自带的防爆破锁定
RECOMMENDED_PREFS = {
    "rss_processing_enabled": True,
    "rss_auto_downloading_enabled": True,
    "web_ui_host_header_validation_enabled": False,
    "web_ui_max_auth_fail_count": 50,
}


@router.post("/qbittorrent/test")
async def test_qbittorrent_connection(payload: QbitTestRequest):
    """用待保存的这份凭据(不是当前已保存的那份)试登录一次,顺便报告当前偏好设置现状。"""
    try:
        cookie = await qbittorrent_client.login_with(
            payload.host, payload.port, payload.username, payload.password
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败: {e}")

    base_url = f"http://{payload.host}:{payload.port}"
    try:
        prefs = await qbittorrent_client.get_preferences(base_url, cookie)
    except Exception:
        prefs = {}

    return {
        "connected": True,
        "current_preferences": {
            key: prefs.get(key) for key in RECOMMENDED_PREFS
        },
    }


@router.post("/qbittorrent/apply")
async def apply_qbittorrent_setup(payload: QbitApplyRequest, db: Session = Depends(get_db)):
    """保存qBittorrent连接信息,可选应用推荐偏好设置。"""
    try:
        cookie = await qbittorrent_client.login_with(
            payload.host, payload.port, payload.username, payload.password
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败,未保存: {e}")

    if payload.apply_recommended:
        base_url = f"http://{payload.host}:{payload.port}"
        try:
            await qbittorrent_client.set_preferences(base_url, cookie, RECOMMENDED_PREFS)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"连接成功,但应用推荐设置失败: {e}")

    upsert_setting(db, "qbit_host", payload.host)
    upsert_setting(db, "qbit_port", str(payload.port))
    upsert_setting(db, "qbit_username", payload.username)
    upsert_setting(db, "qbit_password", payload.password)
    upsert_setting(db, "qbit_setup_completed", "true")

    # 保存的是新凭据,强制丢弃旧缓存,下次调用重新登录用新配置。
    qbittorrent_client.invalidate_session()

    return {"status": "success", "applied_recommended": payload.apply_recommended}
