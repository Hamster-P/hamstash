import httpx
import asyncio
import re
import unicodedata

from services.proxy import get_proxy_url

BASE_URL = "https://api.bgm.tv"
# Bangumi meta_tags 里的非日本产地标签,命中即判为国漫/他国番剧过滤掉。
# 搜索结果按"黑名单"过滤(见search_anime):只剔除明确标了这些产地的,无产地标签的
# 一律保留——否则社区没补产地标签的日本剧场版(如"游戏王剧场版 光之金字塔",meta_tags
# 只有[剧场版,原创])会被误杀。
FOREIGN_ORIGIN_TAGS = {"中国", "中国大陆", "香港", "台湾", "韩国", "美国", "欧美", "英国", "法国"}
HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8"
}
PART_PATTERN = re.compile(
    r'第\s*[0-9一二三四五六七八九十]+\s*部分'      # 第2部分
    r'|后半部分|後半部分|后半|後半|下半部分|下半'    # 后半/下半
    r'|完结篇|完結篇|完結編|完结编'                  # 完结篇(ReLIFE/齐木楠雄的灾难等常用)
    r'|後編|后编'                                    # "前编/后编"两段式命名里表示"后半"的那一半
    r'|第\s*[0-9一二三四五六七八九十]+\s*クール'      # 日文原名的"第2クール"记法(中文译名通常转写成"第2部分")
    r'|part\s*[2-9]',                               # 英文Part 2/PART2这类记法
    re.IGNORECASE,
)
# 注意:只收"表示续播/后半段"的写法(第2部分/后半/完结篇/後編/第2クール/Part2...),
# 不能收"前编/前編"这类"第一段"的写法——resolve_family_season_map依据这个正则判断
# "这个成员是不是接着上一个候选播的续集、不单独占季度序号",如果"前编"也命中,会把
# 一个本该正常拿到新序号的"新一季上半段"错误合并进上一季里,反而引入新的错位。

def _normalize_for_match(text: str) -> str:
    """归一化标题/关键词用于子串匹配:全角标点/字母数字转半角(NFKC)、
    大小写折叠、去除空白,消除纯格式差异导致的误判漏网。
    不做简繁转换(如需要应引入opencc,当前范围内不做)。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", normalized)


async def search_anime(keyword: str, limit: int = 100, year: int | None = None, month: int | None = None, offset: int = 0):
    url = f"{BASE_URL}/v0/search/subjects"
    # 基础过滤器：锁定动画类型 (2代表动画)
    filter_options = {
        "type": [2]
    }

    # 2. 核心修正：将年份和季度转换为官方支持的 air_date 日期区间数组
    if year and str(year) != "不限":
        try:
            y = int(year)
            if month and str(month) != "":
                m = int(month)
                # 根据季度（1月/4月/7月/10月）构建范围
                if m == 1:
                    filter_options["air_date"] = [f">={y}-01-01", f"<={y}-03-31"]
                elif m == 4:
                    filter_options["air_date"] = [f">={y}-04-01", f"<={y}-06-30"]
                elif m == 7:
                    filter_options["air_date"] = [f">={y}-07-01", f"<={y}-09-30"]
                elif m == 10:
                    filter_options["air_date"] = [f">={y}-10-01", f"<={y}-12-31"]
                else:
                    filter_options["air_date"] = [f">={y}-{m:02d}-01", f"<={y}-{m:02d}-28"]
            else:
                # 只传了年份，搜索整年
                filter_options["air_date"] = [f">={y}-01-01", f"<={y}-12-31"]
        except ValueError:
            pass

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
            keyword_norm = _normalize_for_match(keyword)

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

            # 产地过滤用"黑名单"而非"白名单":只剔除meta_tags明确标了非日本产地
            # (中国/韩国/美国...见FOREIGN_ORIGIN_TAGS)的条目,无产地标签的一律保留。
            # 之前用"只留日本/WEB"的白名单有两个毛病:①社区没补产地标签的日本剧场版
            # (如光之金字塔,tags只有[剧场版,原创])被误杀;②国漫普遍也带WEB,反而从
            # WEB放行口漏进来。改黑名单同时修好这两点——国漫靠"中国"标签被准确剔除,
            # 无标签的日本片得以保留。这个字段搜索接口本身就带,不需要额外请求。
            filtered_list = [
                item for item in filtered_list
                if not (FOREIGN_ORIGIN_TAGS & set(item.get("meta_tags") or []))
            ]

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
    已废弃,当前没有调用方。集数偏移量改由services/bgm_series_cache.py::
    build_season_episode_table()按season_ordinal分组汇总AnimeFamilyCache算出——
    那套算法直接读已缓存的家族数据、不发额外网络请求,也不依赖下面这种
    "名字里有没有第X部分"的文本匹配,更可靠。保留这个函数只是为了留个历史参照。

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
