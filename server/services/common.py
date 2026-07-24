"""
最通用的一小撮工具函数：设置读写、路径分段清洗。

代理解析/缓存搬到了services/proxy.py，暂存目录+改名状态记录搬到了
services/staging.py，Bangumi系列/季度解析缓存搬到了services/bgm_series_cache.py——
这个文件只留下没有归属、体量太小不值得单独开文件的通用小工具。
"""
import re

from sqlalchemy.orm import Session

from models import AppSetting


def get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value else default


def upsert_setting(db: Session, key: str, value: str) -> None:
    """按key查询→更新或插入→提交，之前setup.py和settings.py各自写了一份同样的逻辑。"""
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def sanitize_path_segment(text: str) -> str:
    """去掉qBittorrent RSS路径分隔符\\和/以及首尾空白,避免层级错乱。"""
    return re.sub(r"[\\/]+", "_", text).strip() or "未命名"
