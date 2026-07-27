"""对应前端 DownloadManagerPage(下载详情):实时展示qBittorrent里anime-hub分类种子的进度/状态,支持删除。"""
from datetime import datetime

from fastapi import APIRouter

import qbittorrent_client
from schemas import BatchDeleteTasksRequest
from services.staging import ORGANIZE_TAG

router = APIRouter(prefix="/downloads", tags=["下载详情"])

CATEGORY = "anime-hub"

# 属于"下载已完成、种子在做种/等待整理"这一类的qBittorrent原始状态
_COMPLETED_STATES = {
    "uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP", "pausedUP",
}
_ERROR_STATES = {"error", "missingFiles"}


def _map_status(torrent: dict) -> str:
    tags = [t.strip() for t in (torrent.get("tags") or "").split(",")]
    if ORGANIZE_TAG in tags:
        return "已整理"
    state = torrent.get("state", "")
    if state in _ERROR_STATES:
        return "出错"
    if state in _COMPLETED_STATES:
        return "已完成"
    return "下载中"


def _serialize(torrent: dict) -> dict:
    added_on = torrent.get("added_on")
    return {
        "hash": torrent.get("hash"),
        "name": torrent.get("name"),
        "progress": round((torrent.get("progress") or 0) * 100, 1),
        "status_label": _map_status(torrent),
        "size": torrent.get("size", 0),
        "dlspeed": torrent.get("dlspeed", 0),
        "added_on": datetime.fromtimestamp(added_on).isoformat() if added_on else None,
    }


@router.get("/tasks")
async def list_download_tasks():
    """列出anime-hub分类下的全部种子,按添加时间倒序(最新的在最上面)。"""
    torrents = await qbittorrent_client.get_category_torrents(CATEGORY)
    torrents.sort(key=lambda t: t.get("added_on", 0), reverse=True)
    return [_serialize(t) for t in torrents]


async def _delete_one(torrent_hash: str) -> dict:
    matches = await qbittorrent_client.get_torrents_by_hashes([torrent_hash])
    # 有hub-organized标签说明文件已经被整理任务搬进媒体库了,只删种子;
    # 没有标签(还在下载中,或种子已经不存在了)就连文件一起删,不留下载暂存区的垃圾。
    delete_files = True
    if matches:
        tags = [t.strip() for t in (matches[0].get("tags") or "").split(",")]
        delete_files = ORGANIZE_TAG not in tags
    try:
        await qbittorrent_client.delete_torrent(torrent_hash, delete_files=delete_files)
        return {"hash": torrent_hash, "success": True, "delete_files": delete_files}
    except Exception as e:
        return {"hash": torrent_hash, "success": False, "error": str(e)}


@router.delete("/tasks/{torrent_hash}")
async def delete_download_task(torrent_hash: str):
    return await _delete_one(torrent_hash)


@router.post("/tasks/batch-delete")
async def batch_delete_download_tasks(payload: BatchDeleteTasksRequest):
    results = [await _delete_one(h) for h in payload.hashes]
    return {"results": results}


async def _retry_one(torrent_hash: str) -> dict:
    try:
        await qbittorrent_client.recheck_torrent(torrent_hash)
        await qbittorrent_client.resume_torrent(torrent_hash)
        return {"hash": torrent_hash, "success": True}
    except Exception as e:
        return {"hash": torrent_hash, "success": False, "error": str(e)}


@router.post("/tasks/{torrent_hash}/retry")
async def retry_download_task(torrent_hash: str):
    return await _retry_one(torrent_hash)
