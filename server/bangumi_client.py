import httpx
import asyncio
import re
import unicodedata

from services.common import get_proxy_url

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

            raw_data["data"] = filtered_list
            raw_data["total"] = len(filtered_list)
            raw_data["raw_count"] = len(raw_list)  # 这一页Bangumi实际返回的原始条数
            raw_data["bangumi_total"] = bangumi_total  # Bangumi报告的真实匹配总数，供前端翻页判断
        # -------------------------------------------------------------------------
        return raw_data
    
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

async def resolve_family_season_map(main_bgm_id: int | None) -> dict[int, dict]:
    """
    给整个Bangumi关联家族(从main_bgm_id出发能连到的所有成员)算出每个成员是
    "第几个真正的TV季",不看种子标题文本,只信Bangumi关联图谱本身
    (platform+集数+首播日期)。

    纯网络函数,不碰数据库——缓存/持久化是调用方(services/common.py的
    resolve_tv_season_ordinal_cached)的事,这个函数只管"给一个main_bgm_id,
    把整个家族每个成员的结构化信息算出来",保持这个模块一直以来"只管调
    Bangumi API"的定位。

    返回 {bgm_id: {"name", "date", "platform", "eps", "total_episodes",
    "season_ordinal"}},包含家族里的全部成员(不只是够格当季度候选的那些)——
    season_ordinal是"01"/"02"/...或None(不是真季候选,调用方应按platform
    分流到剧场版/OVA/Season 00)。main_bgm_id为空或整个查询失败时返回空字典。

    判定规则(用辉夜大小姐/青春猪头少年/鬼灭之刃/名侦探柯南四个真实系列反复验证过,
    细节见plans目录里这次改动的记录):
    1. 从main_bgm_id出发,BFS遍历关联条目,只顺着"同一部作品的延续/不同呈现形式"这类
       关系类型走(前传/续集/主线故事/不同演绎/总集篇/全集/番外篇),枚举完整家族。
       明确不跟"衍生"/"联动"/"角色出演"/"不同世界观"这类关系——用柯南实测过,不限制
       关系类型的话,BFS会顺着"角色出演"一路连到鲁邦三世、光之美少女、城市猎人这些
       完全不相关的作品上,把季度序号的候选名单搅乱。resolve_root_subject_id判断
       文件夹归属时只认"主线故事"/"前传"两种关系,同样是为了避免这个问题。
    2. 筛出platform=="TV"且集数>=6的条目——区分"一整季"和"短篇特典/被拆成
       分集播出的剧场版"。集数优先用total_episodes,是因为柯南这类仍在连载、
       总集数未知的番,Bangumi的eps字段固定是0(真实数据里柯南eps=0,
       total_episodes=1339),只看eps会把正片本身都判定成集数不够,
       跟AnimeCatalog里其他地方的集数取值顺序(total_episodes优先)保持一致。
    3. 排除"有《不同演绎》关系指向某个剧场版条目"的TV条目——这类是"剧场版剪成
       TV集数重新播出"(比如鬼灭之刃的"无限列车篇TV重制版"),官方季度编号里
       它跟后一部正篇合算一季,不单独占号,否则会导致它之后所有季度错位一位。
    4. 只有通过"前传/续集/主线故事"连进家族的节点才有资格竞选季度序号,根节点
       天然有资格;通过"总集篇/全集/不同演绎/番外篇"连进来的节点只是留在同一个
       家族/文件夹里,不参与编号。"番外篇"字面意思就是"不算在主线编号里"——柯南
       实测过"名侦探柯南：零的日常"(配角安室透的独立番外喜剧,6集)就是通过
       "番外篇"连进来的,曾被误判成Season02,而结构几乎一样的"名侦探柯南：
       犯人犯泽先生"因为Bangumi标的是"衍生"关系(不在同延续白名单里)反而没这问题——
       Bangumi官方对这两个结构相似的作品打标本身就不一致,只能靠这道额外的资格过滤挡住。
    5. 剩下(通过前传/续集/主线故事连进来、且不满足3的排除条件)的按首播日期排序,
       排第几就是第几季,写进对应成员的season_ordinal。

    没有season_ordinal(为None)的成员,调用方应该按platform分流到剧场版/Season 00,
    不能因为拿不到season_ordinal就退回文本猜测逻辑(那正是这整套机制要修的bug本身)。
    """
    if not main_bgm_id:
        return {}

    eps_threshold = 6
    # 防御性上限:柯南这类连载几十年的长篇,光是"续集"类关系就有上百条番外篇/剧场版,
    # 之前三个系列里最大的鬼灭之刃是21个节点,柯南在收紧关系类型后仍然可能上百,
    # 给够余量,真撞上限了也只是漏掉最边缘的几个特典,不影响主线季度序号的正确性。
    max_family_nodes = 300

    # 只走"同一部作品的延续/不同呈现形式"这几种关系,不跟"衍生/联动/角色出演/
    # 不同世界观"——后者会连到完全不相关的作品上(柯南实测会连到鲁邦三世/光之美少女)。
    _SAME_CONTINUITY_RELATIONS = {
        "前传", "续集", "主线故事", "不同演绎", "总集篇", "全集", "番外篇",
    }
    # 这几种关系才有资格竞选季度序号——"总集篇/全集/不同演绎/番外篇"连出去的节点
    # 依然留在同一个家族/文件夹里,但不参与编号,始终落进Season 00。
    # "番外篇"字面意思就是"不算在主线编号里",不能当真季候选:柯南实测过"名侦探柯南：
    # 零的日常"(配角安室透的独立番外喜剧,6集)就是通过"番外篇"连进来的,曾被误判成Season02。
    _SEASON_ELIGIBLE_RELATIONS = {"前传", "续集", "主线故事"}

    visited: set[int] = set()
    season_eligible: set[int] = {main_bgm_id}  # 家族根节点天然有资格
    # 关系是双向记录的:Bangumi对同一对"不同演绎"关系,通常两边各自的关联列表
    # 里都会出现对方,但保险起见两个方向都记一遍,不依赖这个对称性一定成立。
    different_rendition: dict[int, set[int]] = {}

    # 按层并发BFS,不是一个个串行查:柯南这类连载几十年的长篇,光是"续集"类关系
    # 收紧后还有60+个节点,一个个await查relations实测要11秒;改成同一层内
    # 并发查(限流8个,跟get_subject_details_batch同一套节流方式),
    # 一层的耗时约等于这一层里最慢的那一个请求,而不是所有请求耗时总和。
    relations_semaphore = asyncio.Semaphore(8)

    async def _fetch_relations(bgm_id: int) -> tuple[int, list[dict]]:
        async with relations_semaphore:
            try:
                return bgm_id, await get_subject_relations(bgm_id)
            except Exception:
                return bgm_id, []

    current_layer = {main_bgm_id}
    while current_layer and len(visited) < max_family_nodes:
        visited |= current_layer
        results = await asyncio.gather(*(_fetch_relations(bid) for bid in current_layer))

        next_layer: set[int] = set()
        for current, relations in results:
            for r in relations:
                if r.get("type") != 2:  # 只看动画类型关联,书籍/歌曲/OST等噪音跳过
                    continue
                relation_name = r.get("relation")
                if relation_name not in _SAME_CONTINUITY_RELATIONS:
                    continue
                rid = r.get("id")
                if not rid:
                    continue
                if relation_name == "不同演绎":
                    different_rendition.setdefault(current, set()).add(rid)
                    different_rendition.setdefault(rid, set()).add(current)
                if relation_name in _SEASON_ELIGIBLE_RELATIONS:
                    season_eligible.add(rid)
                if rid not in visited:
                    next_layer.add(rid)

        current_layer = next_layer

    details = await get_subject_details_batch(list(visited))

    # main_bgm_id自己的详情都没查到,说明网络压根不通(BFS第一层的关联请求同样会
    # 静默失败,visited这时通常也就只有main_bgm_id自己)——整个当作失败处理,返回空
    # 字典而不是"一个空壳成员"的家族。调用方(resolve_tv_season_ordinal_cached)
    # 见到空字典就不会写缓存,避免网络暂时不通时把一条platform=None的假数据焊死进
    # 缓存表:命中缓存是不会重试的,这条假数据会让这个bgm_id"永久失忆",网络恢复
    # 之后也测不出来。
    if details.get(main_bgm_id) is None:
        return {}

    candidates: list[tuple[int, str]] = []
    for bid in visited:
        if bid not in season_eligible:
            continue
        detail = details.get(bid)
        if not detail or detail.get("platform") != "TV":
            continue
        eps = detail.get("total_episodes") or detail.get("eps") or 0
        if eps < eps_threshold:
            continue
        rendition_targets = different_rendition.get(bid, set())
        if any((details.get(t) or {}).get("platform") == "剧场版" for t in rendition_targets):
            continue
        candidates.append((bid, detail.get("date") or ""))

    candidates.sort(key=lambda item: item[1])
    ordinal_map = {bid: f"{idx:02d}" for idx, (bid, _) in enumerate(candidates, start=1)}

    family_map: dict[int, dict] = {}
    for bid in visited:
        detail = details.get(bid) or {}
        family_map[bid] = {
            "name": detail.get("name_cn") or detail.get("name") or "",
            "date": detail.get("date"),
            "platform": detail.get("platform"),
            "eps": detail.get("eps"),
            "total_episodes": detail.get("total_episodes"),
            "season_ordinal": ordinal_map.get(bid),
        }
    return family_map


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