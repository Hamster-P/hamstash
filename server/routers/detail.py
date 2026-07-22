"""对应前端 DetailPage(详情):只读查询Bangumi详情,不写入数据库。"""
from fastapi import APIRouter

import bangumi_client

router = APIRouter(tags=["详情"])


@router.get("/bangumi/detail/{bgm_id}")
async def get_bangumi_detail_readonly(bgm_id: int):
    """只读查询Bangumi详情,给详情页展示用,不写入数据库。"""
    detail = await bangumi_client.get_subject_detail(bgm_id)
    return {
        "bgm_id": detail.get("id"),
        "title": detail.get("name_cn") or detail.get("name"),
        "title_original": detail.get("name"),
        "summary": detail.get("summary"),
        "cover_url": detail.get("images", {}).get("large", ""),
        "air_date": detail.get("date"),
        "total_eps": detail.get("total_episodes") or detail.get("eps"),
    }
