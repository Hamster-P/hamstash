"""
后台常驻循环:每10秒探测一次library_root这一刻是否可达(纯文件系统检查,
不碰数据库——数据库本身可能就存在library_root下面,连不上的时候没法通过它
反过来判断"库目录是否可达",会变成鸡生蛋问题)。

探测结果只用来回答前端"要不要提示用户当前连不上媒体库",不影响
database.py::_resolve_db_path()实际把数据库指向哪——那边已经完全信任
library_root这个配置值本身,不再因为"此刻是否可达"去切换成别的数据库。
"""
import asyncio
from pathlib import Path

import config_store
from db_migrate import upgrade_db

_accessible = True
_path = ""


def get_status() -> dict:
    return {"accessible": _accessible, "path": _path}


async def library_root_health_loop() -> None:
    global _accessible, _path

    while True:
        candidate_root = config_store.read_ini().get("library_root") or ""
        newly_accessible = bool(candidate_root) and Path(candidate_root).is_dir()

        if newly_accessible and not _accessible:
            # 从连不上变成连上了:补跑一次upgrade_db(建表+补列,幂等操作,
            # 重复跑安全),确保启动时因为连不上被跳过的建表这次真正补上。
            try:
                upgrade_db()
                print(f"[LIBRARY_HEALTH] library_root恢复可达: {candidate_root}")
            except Exception as e:
                print(f"[LIBRARY_HEALTH] library_root恢复可达后补建表失败: {e}")

        _accessible = newly_accessible
        _path = candidate_root

        await asyncio.sleep(10)
