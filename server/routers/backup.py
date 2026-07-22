"""对应前端 SettingsPage 的"备份与迁移"区块:导出/导入anime_hub.db,用于换机迁移。

anime_hub.db这一个文件里已经包含了所有需要跨机器保留的东西
(观看记录/RSS订阅/qBittorrent连接信息/各项设置),不需要额外打包设置文件。
"""
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database import DB_PATH, engine

router = APIRouter(prefix="/backup", tags=["备份与迁移"])


def _validate_backup_file(path: Path) -> None:
    """拦截"传错文件"的情况:必须是合法SQLite文件,且带有anime-hub的关键表。"""
    with open(path, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3\x00"):
        raise ValueError("不是合法的SQLite数据库文件")

    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_setting'"
        ).fetchone()
        if row is None:
            raise ValueError("这不像是anime-hub的备份文件(缺少app_setting表)")
    finally:
        conn.close()


@router.get("/export")
def export_backup():
    """把当前数据库当文件下载返回,文件名带时间戳。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        DB_PATH,
        filename=f"hamstash_backup_{timestamp}.db",
        media_type="application/octet-stream",
    )


@router.post("/import")
async def import_backup(file: UploadFile):
    """上传一份之前导出的备份,校验通过后覆盖当前数据库。
    不做运行时热替换——SQLAlchemy engine/连接池已经绑定旧文件,覆盖后需要重启应用才能生效。
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        tmp_path = Path(tmp.name)
        shutil.copyfileobj(file.file, tmp)

    try:
        try:
            _validate_backup_file(tmp_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 释放当前进程持有的连接池,尽量避免Windows下文件被占用导致覆盖失败。
        engine.dispose()

        try:
            shutil.move(str(tmp_path), DB_PATH)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"导入失败,数据库文件可能正被占用,请重启应用后重试: {e}",
            )
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"status": "success", "message": "导入成功,请重启应用使其生效"}
