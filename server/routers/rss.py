"""对应前端 RssPage(RSS订阅一览):订阅规则的查询/开关切换/删除。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
from database import get_db
from models import SubscriptionRule
from schemas import RssSubscriptionResponse, RssToggleRequest
from services.common import get_setting
from services.subscription import activate_subscription, deactivate_subscription

router = APIRouter(prefix="/rss", tags=["RSS订阅"])


@router.get("/subscriptions", response_model=list[RssSubscriptionResponse])
def list_rss_subscriptions(db: Session = Depends(get_db)):
    """RSS订阅一览:列出所有已创建的订阅规则,供确认/管理页展示。"""
    return (
        db.query(SubscriptionRule)
        .order_by(SubscriptionRule.created_at.desc())
        .all()
    )


@router.put("/subscriptions/{sub_id}/toggle", response_model=RssSubscriptionResponse)
async def toggle_rss_subscription(
    sub_id: int, payload: RssToggleRequest, db: Session = Depends(get_db)
):
    """打开开关:注册/更新qBittorrent里的RSS源与自动下载规则;关闭开关:撤下自动下载规则。"""
    rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == sub_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="订阅不存在")

    rule.enabled = payload.enabled
    db.commit()
    db.refresh(rule)

    if payload.enabled:
        download_root = get_setting(db, "download_root", config_store.DEFAULTS["download_root"])
        await activate_subscription(db, rule, download_root)
    else:
        await deactivate_subscription(db, rule)

    return rule


@router.delete("/subscriptions/{sub_id}")
async def delete_rss_subscription(sub_id: int, db: Session = Depends(get_db)):
    """手动删除一条RSS订阅:从qBittorrent里撤下自动下载规则+RSS源,再删掉数据库记录。"""
    rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == sub_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="订阅不存在")

    if rule.rule_name:
        try:
            await qbittorrent_client.remove_rss_auto_download_rule(rule.rule_name)
        except Exception:
            pass  # 删除是终态操作,qBittorrent那边清不掉也不应该阻止本地记录删除
    if rule.rss_path:
        try:
            await qbittorrent_client.remove_rss_item(rule.rss_path)
        except Exception:
            pass

    db.delete(rule)
    db.commit()
    return {"deleted": True, "id": sub_id}
