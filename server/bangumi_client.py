import httpx
import asyncio
import re
import unicodedata

from services.proxy import get_proxy_url

BASE_URL = "https://api.bgm.tv"
HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8"
}
PART_PATTERN = re.compile(
    r'第\s*[0-9一二三四五六七八九十]+\s*部分|后半部分|後半部分|后半|後半|下半部分|下半'
)

def _normalize_for_match(text: str) -> str:
    """归一化标题/关键词用于子串匹配:全角标点/字母数字转半角(NFKC)、
    大小写折叠、去除空白,消除纯格式差异导致的误判漏网。
    不做简繁转换(如需要应引入opencc,当前范围内不做)。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", normalized)


def _resolve_air_date_bounds(year: int | None, month: int | None) -> tuple[str, str] | None:
    """把年份/季度转成(起始日期, 结束日期)这一对闭区间字符串,年份/季度不合法时返回None。
    v0搜索的filter.air_date、legacy搜索兜底的手工日期过滤共用这一份换算,避免两处逻辑分叉。
    """
    if not year or str(year) == "不限":
        return None
    try:
        y = int(year)
        if month and str(month) != "":
            m = int(month)
            # 根据季度（1月/4月/7月/10月）构建范围
            if m == 1:
                return f"{y}-01-01", f"{y}-03-31"
            elif m == 4:
                return f"{y}-04-01", f"{y}-06-30"
            elif m == 7:
                return f"{y}-07-01", f"{y}-09-30"
            elif m == 10:
                return f"{y}-10-01", f"{y}-12-31"
            else:
                return f"{y}-{m:02d}-01", f"{y}-{m:02d}-28"
        else:
            # 只传了年份，搜索整年
            return f"{y}-01-01", f"{y}-12-31"
    except ValueError:
        return None


async def _search_anime_legacy_fallback(
    keyword: str, year: int | None, month: int | None
) -> list[dict]:
    """v0搜索接口对某些条目的中文标题全文索引有缺口(比如bgm_id=489577"进入花园",
    v0接口无论用全名还是子串关键词查中文标题都是空结果,但同一个接口用英文原名"Enter The
    Garden"能立刻查到,Bangumi更老的legacy搜索接口用中文关键词也能查到——是Bangumi自己
    索引没跟上,不是关键词或过滤器的问题),只在v0"真的一条都没有"时兜底调用一次,
    把结果字段形状换算成跟v0搜索结果一致,让调用方后续处理不用区分数据来源。

    legacy接口没有meta_tags字段,调用方不应该再对这里返回的结果做"日本"产地过滤
    (那层过滤是基于v0结果才有意义的社区标签,legacy结果永远没有,过滤会把兜底结果又清空)。
    """
    if not keyword:
        return []
    url = f"{BASE_URL}/search/subject/{keyword}"
    params: dict = {"type": 2, "responseGroup": "large", "max_results": 25}
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        response = await client.get(url, params=params, headers=HEADERS, timeout=10.0)
        response.raise_for_status()
        legacy_data = response.json()

    legacy_list = legacy_data.get("list") or []
    bounds = _resolve_air_date_bounds(year, month)
    if bounds:
        start, end = bounds
        legacy_list = [
            item for item in legacy_list
            if start <= (item.get("air_date") or "") <= end
        ]

    converted = []
    for item in legacy_list:
        images = item.get("images") or {}
        rating = dict(item.get("rating") or {})
        rating["rank"] = item.get("rank", rating.get("rank", 0))
        converted.append({
            "id": item.get("id"),
            "type": item.get("type"),
            "name": item.get("name") or "",
            "name_cn": item.get("name_cn") or "",
            "summary": item.get("summary") or "",
            "date": item.get("air_date"),
            "images": images,
            "eps": item.get("eps"),
            "total_episodes": item.get("eps_count") or item.get("eps"),
            "rating": rating,
            "collection": item.get("collection") or {},
            # legacy接口不返回meta_tags/platform/nsfw/locked,调用方需要跳过依赖这些
            # 字段的过滤(比如"日本"产地过滤),不能假装它们存在。
            "meta_tags": None,
        })
    return converted


async def search_anime(keyword: str, limit: int = 100, year: int | None = None, month: int | None = None, offset: int = 0):
    url = f"{BASE_URL}/v0/search/subjects"
    # 基础过滤器：锁定动画类型 (2代表动画)
    filter_options = {
        "type": [2]
    }

    # 2. 核心修正：将年份和季度转换为官方支持的 air_date 日期区间数组
    bounds = _resolve_air_date_bounds(year, month)
    if bounds:
        filter_options["air_date"] = [f">={bounds[0]}", f"<={bounds[1]}"]

    payload = {
            "keyword": keyword,
            "sort": "rank",  # 默认按Bangumi排名排序展示,而不是它自带的相关度排序
            "filter": filter_options
        }

    # 控制返回上限数量，这里支持传入 100
    params = {"limit": limit, "offset": offset}

    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        response = await client.post(
            url, json=payload, params=params, headers=HEADERS, timeout=10.0
        )
        response.raise_for_status()

        raw_data = response.json()  # 拿到 Bangumi 返回的带杂质的原始数据

        # ------------------ 核心改造：后端手动清洗（模拟短语/精确匹配） ------------------
        if "data" in raw_data and isinstance(raw_data["data"], list):
            raw_list = raw_data["data"]
            # Bangumi 不管传多大的 limit，单次请求实际固定只返回20条左右，
            # 真正的匹配总数在它自己的 total 字段里（这里先存下来，
            # 下面会用我们过滤后的数量覆盖掉 raw_data["total"]，
            # 所以要在覆盖前保留一份，供前端判断是否还有下一页）。
            bangumi_total = raw_data.get("total", len(raw_list))

            # v0搜索接口对个别条目的中文标题全文索引有缺口(比如bgm_id=489577"进入花园",
            # 关键词/子串/type过滤器不管怎么组合v0都是0结果,但同一接口用英文原名能查到、
            # Bangumi更老的legacy接口用中文关键词也能查到——是Bangumi自己索引没跟上)。
            # 只在v0第一页就真的一条都没有时兜底一次,不影响v0能正常出结果的绝大多数场景,
            # 也不在翻页(offset>0)时触发,避免跟"加载更多"的分页状态搅在一起。
            if not raw_list and bangumi_total == 0 and offset == 0:
                raw_list = await _search_anime_legacy_fallback(keyword, year, month)
                bangumi_total = len(raw_list)

            keyword_norm = _normalize_for_match(keyword)

            matched = []
            if keyword_norm:
                matched = [
                    item for item in raw_list
                    if keyword_norm in _normalize_for_match(item.get("name_cn") or "")
                    or keyword_norm in _normalize_for_match(item.get("name") or "")
                ]
                # 兜底：归一化后强匹配仍然一个都没有，但Bangumi原始结果非空 ->
                # 大概率是格式/别名差异导致的误杀而非真的无关，这时把Bangumi自己
                # 排序好的原始结果整个吐回去，避免“明明搜得到，却是空列表”。
                # 正常情况下（matched非空）仍然只保留强匹配，不把分词噪音混进来。
                filtered_list = matched if matched else raw_list
            else:
                filtered_list = raw_list

            # 默认只保留Bangumi官方meta_tags标了"日本"产地的条目,过滤掉国漫/
            # 其他产地番剧——这个字段搜索接口本身就带,不需要额外请求。放在关键词
            # 过滤(含空匹配兜底)之后,保证不管有没有走兜底逻辑,最终结果都只剩日漫。
            # meta_tags是None(legacy兜底结果,这个接口根本不返回这个字段)时直接放行,
            # 不能当成"没有日本标签"处理,否则兜底查到的结果会在这里被清空回到原点。
            japan_filtered = [
                item for item in filtered_list
                if item.get("meta_tags") is None or "日本" in (item.get("meta_tags") or [])
            ]
            # 兜底：产地过滤把关键词强匹配(matched)的结果也清空了 -> 大概率是这个条目
            # 社区meta_tags还没标全"日本"(常见于海外联合制作/小众NFT宣传短片这类冷门条目,
            # 比如bgm_id=489577"进入花园",标签只有"WEB",没人补"日本"),而不是产地真的
            # 不对。这时优先信任关键词强匹配,不要让一个缺失的社区标签把标题精确命中的
            # 结果也清没——这正是之前"进入花园"搜不到的实际原因。
            # 只在matched非空时回退,keyword为空(纯浏览/推荐场景)或matched本身也是空的
            # 兜底情况下不触发,避免把产地过滤直接形同虚设。
            filtered_list = japan_filtered if japan_filtered or not matched else matched

            raw_data["data"] = filtered_list
            raw_data["total"] = len(filtered_list)
            raw_data["raw_count"] = len(raw_list)  # 这一页Bangumi实际返回的原始条数
            raw_data["bangumi_total"] = bangumi_total  # Bangumi报告的真实匹配总数，供前端翻页判断
        # -------------------------------------------------------------------------
        return raw_data

def normalize_bgm_subject(payload: dict) -> dict:
    """把Bangumi条目详情/搜索结果的原始payload，归一化成本项目统一使用的展示字段。
    标题/简介/封面/集数各自的fallback链，以前在库匹配同步、搜索导入、详情页只读、
    追更时刻表四个入口各写了一份，细节还互相不一致（比如总集数一处只写total_eps、
    一处只写total_episodes），这里统一成一份，四个调用方都改成调这个。
    """
    if not payload:
        return {}
    images = payload.get("images") or {}
    return {
        "bgm_id": payload.get("id"),
        "title": payload.get("name_cn") or payload.get("name") or "未知动漫",
        "title_original": payload.get("name") or "",
        "summary": payload.get("summary") or "暂无简介",
        "cover_url": images.get("large") or images.get("common") or "",
        "air_date": payload.get("date"),
        "total_eps": payload.get("total_episodes") or payload.get("eps") or 0,
    }


async def get_subject_detail(bgm_id: int):
    url = f"{BASE_URL}/v0/subjects/{bgm_id}"

    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        response = await client.get(url, headers=HEADERS, timeout=10.0)
        response.raise_for_status()
        return response.json()

async def get_subject_details_batch(bgm_ids: list[int]):
    """并发查询多个bgm_id的详情,限制并发数避免被Bangumi限流。"""
    semaphore = asyncio.Semaphore(8)

    async def fetch_one(bid: int):
        async with semaphore:
            try:
                detail = await get_subject_detail(bid)
            except Exception:
                detail = None
            return bid, detail

    tasks = [fetch_one(bid) for bid in bgm_ids if bid]
    results = await asyncio.gather(*tasks)
    return dict(results)

async def get_calendar():
    """获取 Bangumi 每日放送 API"""
    url = f"{BASE_URL}/calendar"
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        response = await client.get(url, headers=HEADERS, timeout=15.0)
        response.raise_for_status()
        return response.json()

async def get_subject_relations(bgm_id: int) -> list[dict]:
    """
    查询某个条目的关联条目列表(续集/前传/主线故事/衍生 等)。
    对应 GET /v0/subjects/{subject_id}/subjects
    """
    url = f"{BASE_URL}/v0/subjects/{bgm_id}/subjects"
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        response = await client.get(url, headers=HEADERS, timeout=10.0)
        response.raise_for_status()
        return response.json()


async def resolve_episode_offset(season_bgm_id: int) -> int:
    """
    计算某一季"拆分播出的后半部分",相对完整这一季的集数偏移量。
    纯文本判断,不依赖relation字段具体取值:
    1. 这个条目名字没有"第X部分"字样 -> 不是拆分,偏移量0。
    2. 从关联条目里筛出同样带"第X部分"字样的,组成"同一季拆分家族"。
    3. 按bgm_id升序排序(数字越小≈越早建/播出越早,启发式非官方保证)。
    4. 累加排在自己前面的每个分段的total_eps,即为偏移量。
    """
    try:
        detail = await get_subject_detail(season_bgm_id)
    except Exception:
        return 0

    self_name = detail.get("name_cn") or detail.get("name") or ""
    if not PART_PATTERN.search(self_name):
        return 0

    try:
        relations = await get_subject_relations(season_bgm_id)
    except Exception:
        return 0

    family_ids = {season_bgm_id}
    for r in relations:
        rel_name = r.get("name_cn") or r.get("name") or ""
        if PART_PATTERN.search(rel_name) and r.get("id"):
            family_ids.add(r["id"])

    if len(family_ids) <= 1:
        return 0

    sorted_ids = sorted(family_ids)
    details_map = await get_subject_details_batch(list(family_ids))

    offset = 0
    for fid in sorted_ids:
        if fid == season_bgm_id:
            break
        d = details_map.get(fid)
        if d:
            offset += d.get("total_episodes") or d.get("eps") or 0
    return offset
