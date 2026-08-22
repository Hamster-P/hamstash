"""订阅规则的一次性数据迁移。

放在独立模块而不是塞进 db_migrate:那边只负责 schema(建表/补列),这里改的是
业务数据,两件事的回滚代价和审阅重点都不一样。
"""
from sqlalchemy.orm import Session

from models import SubscriptionRule
from services.common import get_setting, upsert_setting

# 被重置成"不限"的 animegarden 订阅条数,供 RSS 一览页显示一次性提示。
# 存在已有的 app_setting 表里,不新增表也不新增列。
FANSUB_RESET_COUNT_KEY = "animegarden_fansub_reset_count"

_UNKNOWN_FANSUB = "未知字幕组"


def reset_unknown_fansub_rules(db: Session) -> int:
    """把 source=animegarden 且字幕组是"未知字幕组"的订阅重置成"不限"(NULL),
    返回改动条数。幂等——改完就没有行匹配了,重复启动不会反复刷提示。

    为什么必须改:AnimeGarden 的 fansub 字段只收录它自己登记过的字幕组,个人发布
    或未登记的组一律是 null,组名只出现在 publisher 里。适配器过去只看 fansub,
    这批资源就全被标成"未知字幕组";用户想订阅它们时,唯一能选的也只有这个伪组名,
    靠 matches_criteria 的 field_hit(item.fansub_name == "未知字幕组")命中,
    **今天是能正常工作的**。

    现在适配器改成用 publisher 兜出真实组名(比如"晚街与灯"),field_hit 再也不会
    等于"未知字幕组",这些订阅会**静默停止下载**——不报错、不提示,用户只会发现
    新集数再也没自动下过。所以升级时必须主动把它们放宽成"不限",宁可多抓一点,
    也不能让订阅悄悄变哑。

    **只动 animegarden。** dmhy 的"未知字幕组"是页面解析不出组名时的兜底
    (见 sources/dmhy.py),语义是"这条资源标题里就没写字幕组",跟本次改动无关,
    它的订阅仍然靠 field_hit 正常工作,动了反而是误伤。
    """
    rules = (
        db.query(SubscriptionRule)
        .filter(
            SubscriptionRule.source == "animegarden",
            SubscriptionRule.fansub_name == _UNKNOWN_FANSUB,
        )
        .all()
    )
    if not rules:
        return 0

    for rule in rules:
        rule.fansub_name = None
    db.commit()

    # 累加而不是覆盖:极端情况下(用户从很老的版本分几次升级)可能触发多次,
    # 提示里的条数应该是"总共被重置了多少条",不是"最后一次重置了多少条"。
    try:
        previous = int(get_setting(db, FANSUB_RESET_COUNT_KEY, "0"))
    except (TypeError, ValueError):
        previous = 0
    upsert_setting(db, FANSUB_RESET_COUNT_KEY, str(previous + len(rules)))

    print(f"[MIGRATE] 已把 {len(rules)} 条 AnimeGarden 订阅的字幕组从"
          f"「{_UNKNOWN_FANSUB}」重置为「不限」(该标识已不再产生)")
    return len(rules)
