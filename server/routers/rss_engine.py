"""
对应前端 RssPage(RSS订阅一览)——新版RSS引擎接口,替代routers/rss.py。

跟旧的routers/rss.py是两套独立实现,故意不共享代码:这一版订阅的"开关/删除"
只是纯数据库操作,不再往qBittorrent的RSS订阅树/自动下载规则里注册任何东西——
实际的抓取/匹配/下载全部由services/rss_poller.py的后台轮询循环(或这里的
"立即更新"接口)负责,详见该文件顶部的说明。
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

import config_store
from database import get_db
from models import RssMatchedItem, SubscriptionRule
from schemas import RssMatchedItemResponse, RssSubscriptionResponse, RssToggleRequest
from services import rss_poller
from services.common import get_setting, upsert_setting
from services.rss_migration import FANSUB_RESET_COUNT_KEY
from services.rss_poller import poll_subscription

router = APIRouter(prefix="/rss-engine", tags=["RSS订阅"])


@router.get("/status")
def rss_status(db: Session = Depends(get_db)):
    """RSS 页顶部状态:
    - message: 上次 RSS 轮询遇到的前置障碍(代理/qBittorrent 不可达),空字符串=正常。
      只读后台全局变量、不发任何网络请求。
    - notice: 升级时做过的一次性数据迁移提示,空字符串=没有。跟 message 分开返回,
      因为 message 是每轮轮询重算的瞬时状态,而 notice 要一直留到用户点掉为止。

    RssPage 进入时读一次即可。"""
    return {
        "message": rss_poller.get_last_poll_status(),
        "notice": _fansub_reset_notice(db),
    }


def _fansub_reset_notice(db: Session) -> str:
    """AnimeGarden 字幕组重置迁移的一次性提示文案,没有要提示的就返回空串。"""
    try:
        count = int(get_setting(db, FANSUB_RESET_COUNT_KEY, "0"))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return ""
    return (
        f"本次升级已把 {count} 条 AnimeGarden 订阅的字幕组从「未知字幕组」重置为「不限」。"
        "原因:这些资源的字幕组名此前识别不出来,现在能正确显示了(比如「晚街与灯」),"
        "旧的「未知字幕组」标识不再产生,继续用它会导致订阅抓不到任何内容。"
        "如需重新限定字幕组,请在下载页重新订阅。"
    )


@router.post("/status/dismiss-notice")
def dismiss_notice(db: Session = Depends(get_db)):
    """用户点掉一次性提示:清零计数,之后不再返回 notice。"""
    upsert_setting(db, FANSUB_RESET_COUNT_KEY, "0")
    return {"ok": True}


@router.post("/refresh-all")
async def refresh_all_sources(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """"更新所有 RSS 源"按钮:同步重查代理/qBittorrent 可达性(立即更新红字),
    若无障碍则在后台真正重跑一轮全部启用订阅的抓取。返回最新的状态消息(空=已恢复正常)。"""
    sources = [
        row[0]
        for row in db.query(SubscriptionRule.source)
        .filter(SubscriptionRule.enabled == True)  # noqa: E712
        .distinct()
        .all()
    ]
    qb_ok, unreachable = await rss_poller.check_prerequisites(sources)
    if qb_ok and not unreachable:
        background_tasks.add_task(rss_poller._poll_all_subscriptions)
    return {"message": rss_poller.get_last_poll_status()}


@router.get("/subscriptions", response_model=list[RssSubscriptionResponse])
def list_subscriptions(db: Session = Depends(get_db)):
    """RSS订阅一览:列出所有已创建的订阅规则,供确认/管理页展示。"""
    return db.query(SubscriptionRule).order_by(SubscriptionRule.created_at.desc()).all()


@router.put("/subscriptions/{sub_id}/toggle", response_model=RssSubscriptionResponse)
def toggle_subscription(sub_id: int, payload: RssToggleRequest, db: Session = Depends(get_db)):
    """打开/关闭这条订阅是否参与后台轮询——纯数据库操作,不涉及任何外部调用,不会失败。"""
    rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == sub_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="订阅不存在")

    rule.enabled = payload.enabled
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, db: Session = Depends(get_db)):
    """
    删除一条订阅:只删数据库记录(连同它的命中历史),不涉及任何qBittorrent调用——
    新架构下从来没有往qBittorrent的RSS订阅树里注册过东西,天然不存在"清理失败
    留下孤儿"这类问题。
    """
    rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == sub_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="订阅不存在")

    db.query(RssMatchedItem).filter(RssMatchedItem.subscription_id == sub_id).delete()
    db.delete(rule)
    db.commit()
    return {"deleted": True, "id": sub_id}


@router.get("/subscriptions/{sub_id}/matched-items", response_model=list[RssMatchedItemResponse])
def list_matched_items(sub_id: int, db: Session = Depends(get_db)):
    """给订阅一览页的"命中记录"展示用,按匹配时间倒序。"""
    rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == sub_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="订阅不存在")

    return (
        db.query(RssMatchedItem)
        .filter(RssMatchedItem.subscription_id == sub_id)
        .order_by(RssMatchedItem.matched_at.desc())
        .all()
    )


@router.post("/subscriptions/{sub_id}/refresh-now")
async def refresh_subscription_now(sub_id: int, db: Session = Depends(get_db)):
    """"立即更新"按钮:不等自动轮询周期,马上对这一条订阅跑一轮抓取+匹配+下载。"""
    rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == sub_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="订阅不存在")

    download_root = get_setting(db, "download_root", config_store.DEFAULTS["download_root"])
    await poll_subscription(db, rule, download_root)
    return {"refreshed": True, "id": sub_id}
