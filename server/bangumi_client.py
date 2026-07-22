import httpx
import asyncio
import re

BASE_URL = "https://api.bgm.tv"
HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8"
}
PART_PATTERN = re.compile(
    r'第\s*[0-9一二三四五六七八九十]+\s*部分|后半部分|後半部分|后半|後半|下半部分|下半'
)

async def search_anime(keyword: str, limit: int = 100, year: int | None = None, month: int | None = None):
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
            "filter": filter_options
        }
    
    # 控制返回上限数量，这里支持传入 100
    params = {"limit": limit}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, json=payload, params=params, headers=HEADERS, timeout=10.0
        )
        response.raise_for_status()

        raw_data = response.json()  # 拿到 Bangumi 返回的带杂质的原始数据
        
        # ------------------ 核心改造：后端手动清洗（模拟短语/精确匹配） ------------------
        if "data" in raw_data and isinstance(raw_data["data"], list):
            filtered_list = []
            keyword_lower = keyword.lower().strip()
            
            for item in raw_data["data"]:
                name_cn = (item.get("name_cn") or "").lower()
                name_en = (item.get("name") or "").lower()
                
                # 强匹配逻辑：只有当动漫的“中文名”或“原名”中，完整包含了用户输入的连续字符串，才保留
                # 这样就能完美剔除像“泡芙小姐”（不包含芙丽莲）这种由于分词被意外带出来的无关垃圾数据
                if keyword_lower in name_cn or keyword_lower in name_en:
                    filtered_list.append(item)
            
            # 将清洗完的纯净结果重新塞回去
            raw_data["data"] = filtered_list
            raw_data["total"] = len(filtered_list)
        # -------------------------------------------------------------------------
        return raw_data
    
async def get_subject_detail(bgm_id: int):
    url = f"{BASE_URL}/v0/subjects/{bgm_id}"

    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=HEADERS, timeout=15.0)
        response.raise_for_status()
        return response.json()

async def get_subject_relations(bgm_id: int) -> list[dict]:
    """
    查询某个条目的关联条目列表(续集/前传/主线故事/衍生 等)。
    对应 GET /v0/subjects/{subject_id}/subjects
    """
    url = f"{BASE_URL}/v0/subjects/{bgm_id}/subjects"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=HEADERS, timeout=10.0)
        response.raise_for_status()
        return response.json()


async def resolve_root_subject_id(bgm_id: int, max_depth: int = 20) -> int:
    """
    从系列里任意一部(某一季/某一部剧场版)出发,顺着Bangumi的关联关系,
    找到这个系列最早的那个条目ID,用来统一整部作品在媒体库里的文件夹归属。

    判定顺序(每个节点都按这个顺序检查):
    1. 有"主线故事"关联 -> 优先跳过去,不管这个节点自己有没有"前传"。
       这是为了应对剧场版场景:剧场版之间的"前传"经常互相指来指去形成环
       (比如3部剧场版循环套娃),必须先跳出这个环境、回到它所属的正篇系列,
       才能开始可靠地继续向前追溯。
    2. 没有"主线故事",但有"前传" -> 跳到前传,回到步骤1继续判断
       (递归/循环地一直往前找)。
    3. 既没有"主线故事"也没有"前传" -> 这就是系列里最早的条目,停止。

    用visited集合记录走过的所有id,一旦下一跳的目标已经走过,
    视为检测到环,立即停止在当前节点,不再继续跳,避免死循环挂死。
    """
    current = bgm_id
    visited: set[int] = set()

    for _ in range(max_depth):
        if current in visited:
            break  # 检测到环,current就是能确定的最上游节点,停止
        visited.add(current)

        try:
            relations = await get_subject_relations(current)
        except Exception:
            break  # 查询失败不影响主流程,就用当前已知的这个id兜底

        main_story = next(
            (r for r in relations if r.get("relation") == "主线故事"), None
        )
        if main_story and main_story.get("id") not in visited:
            current = main_story["id"]
            continue

        prequel = next(
            (r for r in relations if r.get("relation") == "前传"), None
        )
        if prequel and prequel.get("id") not in visited:
            current = prequel["id"]
            continue

        break  # 两者都没有(或者都已经走过了),此即系列最早的条目

    return current

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