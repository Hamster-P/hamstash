"""
Bangumi关联家族/季度解析算法。

不是简单的HTTP客户端封装——这里是图遍历+分类规则的业务逻辑(BFS遍历关联条目、
判定谁是系列根节点、谁排第几季),只是恰好全部数据来源于bangumi_client.py那几个
薄HTTP封装。从bangumi_client.py拆出来,是因为这部分逻辑(~300行)体量和复杂度都
远超"客户端"该有的范围,值得单独一个文件;对bangumi_client.py保持单向依赖,
不会产生循环import。
"""
import asyncio

import bangumi_client

# 只走"同一部作品的延续/不同呈现形式"这几种关系,不跟"衍生/联动/角色出演/
# 不同世界观"——后者会连到完全不相关的作品上(柯南实测会连到鲁邦三世/光之美少女)。
# resolve_root_subject_id的兜底家族扫描和resolve_family_season_map的家族枚举
# 共用同一份白名单,避免两处定义漂移。
_SAME_CONTINUITY_RELATIONS = {
    "前传", "续集", "主线故事", "不同演绎", "总集篇", "全集", "番外篇",
}

# 根节点兜底扫描用的集数门槛:跟resolve_family_season_map里"季度候选"用的
# eps_threshold(=6)是两个独立常量,语义不同(一个是"够格当整个家族的锚点",
# 一个是"够格当季度候选"),不合并复用。
_ROOT_CANDIDATE_EPS_THRESHOLD = 10


async def _scan_family_tv_root_candidates(start_bgm_id: int) -> list[int]:
    """
    从start_bgm_id出发,顺着_SAME_CONTINUITY_RELATIONS白名单做一次前向BFS,
    枚举整个关联家族,返回其中platform=="TV"且集数(total_episodes优先)超过
    _ROOT_CANDIDATE_EPS_THRESHOLD的bgm_id列表——只用来给resolve_root_subject_id
    的兜底逻辑挑候选根节点,不做season_eligible/不同演绎排除这些季度分配相关的
    追踪,比resolve_family_season_map内部那个BFS更轻量。

    只有resolve_root_subject_id的链式回溯本身失败时才会调用这个函数(见其文档),
    正常单一延续的系列不会触发,不会给这些系列增加额外网络开销。
    """
    max_family_nodes = 300
    semaphore = asyncio.Semaphore(8)

    async def _fetch_relations(bgm_id: int) -> tuple[int, list[dict]]:
        async with semaphore:
            try:
                return bgm_id, await bangumi_client.get_subject_relations(bgm_id)
            except Exception:
                return bgm_id, []

    visited: set[int] = set()
    current_layer = {start_bgm_id}
    while current_layer and len(visited) < max_family_nodes:
        visited |= current_layer
        results = await asyncio.gather(*(_fetch_relations(bid) for bid in current_layer))
        next_layer: set[int] = set()
        for _current, relations in results:
            for r in relations:
                if r.get("type") != 2:
                    continue
                if r.get("relation") not in _SAME_CONTINUITY_RELATIONS:
                    continue
                rid = r.get("id")
                if rid and rid not in visited:
                    next_layer.add(rid)
        current_layer = next_layer

    details = await bangumi_client.get_subject_details_batch(list(visited))

    candidates: list[int] = []
    for bid in visited:
        detail = details.get(bid)
        if not detail:
            continue
        # 详情接口跟随了重定向(条目被Bangumi合并进了另一个canonical id,
        # 跟resolve_family_season_map里处理的126199->18692是同一类问题),
        # 按canonical id算,不把alias自己当独立候选。
        canonical_id = detail.get("id") or bid
        if detail.get("platform") != "TV":
            continue
        eps = detail.get("total_episodes") or detail.get("eps") or 0
        if eps > _ROOT_CANDIDATE_EPS_THRESHOLD:
            candidates.append(canonical_id)

    return candidates


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
    3. 都没有,但有"全集" -> 跳到"全集"指向的条目。这是给总集篇/剪辑版这类
       条目用的:实测航海王的"东海篇~路飞与四名伙伴的大冒险~"(2017年播出的
       总集篇特别版,bgm_id=217886)跟正片之间只有"全集"关系,没有"前传"/
       "主线故事",不加这一步会把217886自己误判成根节点,真正的正片(bgm_id=975,
       1999年开播)反而被排除在外。注意"全集"和"总集篇"是同一对关系的两个方向、
       不能都跳:总集篇条目指向正片时用"全集"("我是XX的全集"),正片指向自己
       衍生出的总集篇时用"总集篇"("XX是我的总集篇")——用鬼灭之刃/航海王两个
       系列的真实数据交叉验证过这个方向规律。只跳"全集"能正确从总集篇走回正片,
       如果反过来也跳"总集篇",会从正片(比如975自己有9条"总集篇"关联)反向
       走进它自己的衍生总集篇里,方向整个走反。
    4. 都没有(或者都已经走过了) -> 这就是系列最早的条目,停止。

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
            relations = await bangumi_client.get_subject_relations(current)
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

        compiled_from = next(
            (r for r in relations if r.get("relation") == "全集"), None
        )
        if compiled_from and compiled_from.get("id") not in visited:
            current = compiled_from["id"]
            continue

        break  # 都没有(或者都已经走过了),此即系列最早的条目

    # 兜底:链式回溯落脚点(current)本身应该是一部"看起来像正片"的TV条目——
    # 如果不是(比如落在了一部电影上),说明这个入口自己缺少指回正片的关联声明
    # (真实案例:哆啦A梦的"2112年哆啦A梦的诞生"电影bgm_id=121745,自己完全没有
    # "主线故事"关联,只有别的条目单方面声明它是自己的衍生,链式回溯从121745出发
    # 无路可退,会把121745自己错当成整个家族的根节点)。这种情况下,在入口所在的
    # 整个关联家族里重新找一个更合理的锚点:所有platform=="TV"且集数够多的候选里,
    # 取bgm_id数值最小的那个——bgm_id是Bangumi条目入库时分配的自增编号,取最小值
    # 能保证这个选择长期稳定,不会因为家族以后又收录新成员而漂移。
    #
    # 只有链式回溯本身"跑偏"了才会走到这里,正常单一延续的系列(根节点本来就是
    # 够格的TV正片)在下面这个detail查询就会直接命中返回,不会触发更贵的家族扫描。
    try:
        current_detail = await bangumi_client.get_subject_detail(current)
    except Exception:
        return current

    current_eps = current_detail.get("total_episodes") or current_detail.get("eps") or 0
    if current_detail.get("platform") == "TV" and current_eps > _ROOT_CANDIDATE_EPS_THRESHOLD:
        return current

    try:
        candidates = await _scan_family_tv_root_candidates(bgm_id)
    except Exception:
        return current

    if not candidates:
        # 整个家族里都没有够格的TV正片(比如纯剧场版系列),没有更好的选择,
        # 保底沿用链式回溯的原始落脚点,不引入新的假设。
        return current

    return min(candidates)


async def resolve_family_season_map(main_bgm_id: int | None) -> dict[int, dict]:
    """
    给整个Bangumi关联家族(从main_bgm_id出发能连到的所有成员)算出每个成员是
    "第几个真正的TV季",不看种子标题文本,只信Bangumi关联图谱本身
    (platform+集数+首播日期)。

    纯网络函数,不碰数据库——缓存/持久化是调用方(services/bgm_series_cache.py的
    resolve_tv_season_ordinal_cached)的事,这个函数只管"给一个main_bgm_id,
    把整个家族每个成员的结构化信息算出来"。

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
    3. 一组《不同演绎》关系里,**首播更晚的那个是重制/剪辑版,不占季号**——比如
       鬼灭之刃"无限列车篇TV版"(2021-10)是2020-10那部剧场版的重播剪辑,高达SEED
       "HD重制版"(2012)是2002年原版的重制。反过来,先出的原版永远保留资格:机动战士
       Z高达(1985)不会被它2005年才上映的剧场版剪辑版挤掉。判据是日期先后而不是
       对方的platform,详见下面这段判断处的注释。
    4. 只有通过"前传/续集/主线故事"连进家族的节点才有资格竞选季度序号,根节点
       天然有资格;通过"总集篇/全集/不同演绎/番外篇"连进来的节点只是留在同一个
       家族/文件夹里,不参与编号。"番外篇"字面意思就是"不算在主线编号里"——柯南
       实测过"名侦探柯南:零的日常"(配角安室透的独立番外喜剧,6集)就是通过
       "番外篇"连进来的,曾被误判成Season02,而结构几乎一样的"名侦探柯南:
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
    # 用的是模块级的_SAME_CONTINUITY_RELATIONS(resolve_root_subject_id的兜底
    # 家族扫描也用同一份,避免两处定义漂移)。
    # 这几种关系才有资格竞选季度序号——"总集篇/全集/不同演绎/番外篇"连出去的节点
    # 依然留在同一个家族/文件夹里,但不参与编号,始终落进Season 00。
    # "番外篇"字面意思就是"不算在主线编号里",不能当真季候选:柯南实测过"名侦探柯南:
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
                return bgm_id, await bangumi_client.get_subject_relations(bgm_id)
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

    details = await bangumi_client.get_subject_details_batch(list(visited))

    # Bangumi条目合并去重:某个bgm_id的详情接口被服务端重定向到另一个canonical id时
    # (旧条目被官方合并进新条目,比如哆啦A梦121739的"主线故事"目标126199实际上是
    # 18692的旧ID,GET /v0/subjects/126199会302到18692),get_subject_detail因为
    # follow_redirects=True会静默拿回canonical id的数据,但返回字典仍以调用方传入的
    # 旧id为key——不去重的话BFS会把同一部作品的镜像id当成两个独立家族成员,携带完全
    # 相同的数据,其中一个还可能意外抢到季度候选资格(镜像id是通过"主线故事"这类关系
    # 发现的,而canonical id本尊却是通过"不同演绎"这类不参与候选的关系发现的,真实
    # 案例见plans里哆啦A梦这一段记录)。
    alias_to_canonical: dict[int, int] = {}
    for bid in list(visited):
        detail = details.get(bid)
        canonical_id = detail.get("id") if detail else None
        if canonical_id and canonical_id != bid:
            alias_to_canonical[bid] = canonical_id

    for alias_id, canonical_id in alias_to_canonical.items():
        visited.discard(alias_id)
        alias_detail = details.pop(alias_id, None)
        if canonical_id not in visited:
            # canonical本身不在这次遍历到的家族里(理论上少见),把这个alias的
            # 详情按canonical id收编,当成一个新成员看待。
            visited.add(canonical_id)
            if alias_detail is not None:
                details[canonical_id] = alias_detail
        if alias_id in season_eligible:
            season_eligible.discard(alias_id)
            season_eligible.add(canonical_id)

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
        # "不同演绎"排除规则:一组"不同演绎"关系里,**出得晚的那个是重制/剪辑版,
        # 不占季号;出得早的是原版,保留**。判据是首播日期的先后,不是对方的platform。
        #
        # 之前这里写的是"只要有《不同演绎》指向某个剧场版条目就排除",实测误伤了两类:
        # 1. 机动战士Z高达(9622, 1985年首播的50集正片)的三部「不同演绎」剧场版是
        #    2005-2006年**后出的**剪辑版,方向跟规则本意刚好相反,结果把正片自己
        #    排除了,ZZ 顶替拿走 Season 02,而 Z高达 掉进 Season 00(播放器当特典)。
        # 2. 高达SEED家族的"HD重制版"跟原版之间是 TV↔TV 的「不同演绎」,旧判据只看
        #    platform=="剧场版"根本命中不了,于是两部重制版白占了 Season 03/04。
        #
        # 换成看先后之后,原本要挡的目标依然挡得住:鬼灭之刃「无限列车篇」TV版
        # (7集, 2021-10)是 2020-10 那部剧场版的重播剪辑,出得更晚,照样被排除。
        # 航海王那个反例(975正片 <-> 2000号剧场版《乔巴身世之谜》)也自然成立——
        # 正片1999年比剧场版早,不会再被自己的衍生剧场版排除掉;下面那道
        # bid != main_bgm_id 的护栏保留,家族根节点永远有资格。
        if bid != main_bgm_id:
            my_date = detail.get("date") or ""
            is_later_rendition = False
            for t in different_rendition.get(bid, set()):
                target = details.get(t) or {}
                target_date = target.get("date") or ""
                if not my_date or not target_date:
                    continue  # 缺日期无法判先后,不做排除(宁可多留一个候选)
                if my_date > target_date and target.get("platform") in ("剧场版", "TV"):
                    is_later_rendition = True
                    break
            if is_later_rendition:
                continue
        candidates.append((bid, detail.get("date") or ""))

    candidates.sort(key=lambda item: item[1])

    # 按首播日期排好序后不能无脑连续编号:同一季拆成两段播出时(比如"第2部分"/
    # "後編"/日文原名的"第2クール"),Bangumi会把后半段建成独立的family成员,
    # 但它不是一个新季,不该单独占用一个季度序号——否则后面每一部真正的新季都会
    # 被顺移(无职转生第一季/第二季各自拆两段播出,实测下来会把2026年才开播的
    # 第三季错误编号成"05"而不是"03")。
    #
    # 两条并列的"这是续播段"判据:
    # 1. bangumi_client.PART_PATTERN —— 认"第2部分/后半/完结篇/後編/第2クール/Part2"
    #    这些固定后缀词。
    # 2. 显式季号跟前一个候选相同 —— 分段的命名其实是自由的,Re:Zero第三季拆成
    #    「袭击篇/反击篇」、第四季拆成「丧失篇/夺还篇」,一个后缀词都不命中,于是
    #    每段各占一个季号,第四季被顺移编成了Season 06(用户实际下载时撞上的)。
    #    但这些分段的标题里都明写着「第三季」「第四季」,直接比这个就行。
    #
    # 只跟**按日期紧邻的前一个候选**比,不跟"目前这一季的季号"比:家族里如果有两部
    # 不相邻却都写着"第二季"的作品(不同世界观的重启作之类),不会被跨越中间的季度
    # 错误粘合在一起。
    #
    # 显式季号只参与"合不合并"的判断,**不拿来当季号本身**——序号仍然是按首播日期
    # 数出来的,所以即使Bangumi某个标题的季号写错,也只影响合并,不会在序号序列里
    # 凿出空洞(build_season_episode_table的偏移量累加依赖序号连续)。
    #
    # 反面样本:鬼灭之刃的「游郭篇/刀匠村篇/柱训练篇」是真正独立的季,它们的标题里
    # 没有"第N季",两条判据都不命中,仍然各占一个季号——所以这里绝不能泛化成
    # "带'篇'字就合并"。
    ordinal_map: dict[int, str] = {}
    current_ordinal = 0
    previous_explicit_season: int | None = None
    for bid, _date in candidates:
        detail = details.get(bid) or {}
        name = detail.get("name_cn") or detail.get("name") or ""
        explicit_season = bangumi_client.parse_explicit_season(name)

        is_continuation = current_ordinal > 0 and (
            bool(bangumi_client.PART_PATTERN.search(name))
            or (explicit_season is not None and explicit_season == previous_explicit_season)
        )
        if not is_continuation:
            current_ordinal += 1
        ordinal_map[bid] = f"{current_ordinal:02d}"
        previous_explicit_season = explicit_season

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


async def has_new_family_members(known_bgm_ids: set[int]) -> bool:
    """
    给"补番"功能用的轻量核实:只查known_bgm_ids里每一个成员自己的relations(),
    看有没有一条_SAME_CONTINUITY_RELATIONS白名单内的关系指向一个不在
    known_bgm_ids里的id——不做BFS多层递归,一轮并发就完事。

    resolve_family_season_map的BFS对家族里每个成员的relations()总共也只查一次
    (访问过的节点不会重复查),所以"全量BFS"和"只查已知成员"两者发出的网络请求
    数量级相同,真正的差异在编排方式:全量BFS必须一层层顺序进行(不知道下一层
    有哪些节点之前没法发下一层的请求),而已知成员集合可以一轮并发把relations()
    一次查完。命中缓存的家族(已知成员已经确定)用这个函数替代重新跑一遍全量BFS,
    没发现新内容就直接信缓存,发现了就交给调用方触发resolve_family_season_map
    完整重算——补的是"缓存可能是旧的,但补番就是要发现新出的续作"这个缺口。
    """
    semaphore = asyncio.Semaphore(8)

    async def _check(bgm_id: int) -> bool:
        async with semaphore:
            try:
                relations = await bangumi_client.get_subject_relations(bgm_id)
            except Exception:
                return False
        for r in relations:
            if r.get("type") != 2:
                continue
            if r.get("relation") not in _SAME_CONTINUITY_RELATIONS:
                continue
            rid = r.get("id")
            if rid and rid not in known_bgm_ids:
                return True
        return False

    results = await asyncio.gather(*(_check(bid) for bid in known_bgm_ids))
    return any(results)
