"""
把下载页里选择的筛选条件(关键词/字幕组/画质/字幕语言/格式/单集或合集),
翻译成一条qBittorrent"自动下载规则"能识别的正则表达式,
让RSS命中的范围尽量贴近用户在搜索页设置的筛选结果。
"""
import shlex

from models import SubscriptionRule


def _split_keyword(keyword: str) -> list[str]:
    """按空格切词,但支持双引号包起来的短语当一个词(引号本身去掉)——
    比如nyaa/dmhy搜索页教用户用 "Season 3" 这种带引号的精确短语去避开"3"这种
    单个数字误伤"第X集"里的阿拉伯数字,这里如果只是朴素split(),引号会被当成
    普通字符切进词里,变成要求标题里必须原样出现带引号的字符串,永远匹配不到,
    等于把这条订阅规则废掉。用shlex.split()识别引号语法,跟搜索页的用法保持一致。
    """
    try:
        return shlex.split(keyword)
    except ValueError:
        # 引号没配对之类的解析不了,原样按空格切,不因为这个直接崩溃
        return keyword.split()


def _alt_group(terms: list[tuple[str, bool]], ignorecase: bool = True) -> str:
    """
    把一组"可选项"拼成正则的非捕获分组,如 (?i:简|chs|gb)。
    terms里每一项是(文本, 是否是已经写好的正则片段)的元组;
    普通文本会被re.escape转义,已经是正则片段的(比如 \\d+-\\d+)原样保留。
    """
    import re

    escaped = [text if is_raw else re.escape(text) for text, is_raw in terms]
    body = "|".join(escaped)
    flag = "i:" if ignorecase else ":"
    return f"(?{flag}{body})"


def _require_any(terms: list[tuple[str, bool]], ignorecase: bool = True) -> str:
    """要求标题命中terms里任意一项:正向预查 (?=.*(?i:a|b|c))"""
    return f"(?=.*{_alt_group(terms, ignorecase)})"


def _forbid_any(terms: list[tuple[str, bool]], ignorecase: bool = True) -> str:
    """要求标题不能命中terms里任意一项:负向预查 (?!.*(?i:a|b|c))"""
    return f"(?!.*{_alt_group(terms, ignorecase)})"


def _subtitle_terms(value: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """
    字幕语言筛选 -> (必须包含的候选词列表, 必须排除的候选词列表)。
    每个候选词元组为 (关键词, 是否正则)，这里都用普通文本匹配 (False)。
    映射逻辑严格对齐前端，确保本地过滤与后端 RSS 订阅结果绝对一致。
    """
    # 提取公共的基础特征词，方便后面组合
    simplified_terms = [("简", False), ("chs", False), ("gb", False)]
    traditional_terms = [("繁", False), ("cht", False), ("big5", False)]
    japanese_terms = [("日", False), ("jp", False), ("japanese", False)]
    raw_terms = [("raw", False), ("bilibili", False), ("baha", False), ("cr", False), ("crunchyroll", False), ("web-dl", False)]

    # 1. 纯简体：必须包含简体特征；绝对不能包含繁体、简日、以及各种无字蓝光/WEB源特征
    if value == "纯简体":
        must_contain = simplified_terms
        must_exclude = traditional_terms + [("简日", False)] + raw_terms
        return must_contain, must_exclude

    # 2. 繁体：必须包含繁体特征；绝对不能包含简体、繁日、以及各种无字蓝光/WEB源特征
    elif value == "繁体":
        must_contain = traditional_terms
        must_exclude = simplified_terms + [("繁日", False)] + raw_terms
        return must_contain, must_exclude

    # 3. 简繁：必须同时包含“简繁”这个词（或者根据你的后端逻辑，如果必须同时包含简和繁，
    # 可以在后续拼正则时对 must_contain 里的词做交集处理。这里按前端显式包含“简繁”或双特征提供候选）
    elif value == "简繁":
        must_contain = [("简繁", False)] + simplified_terms + traditional_terms
        must_exclude = []
        return must_contain, must_exclude

    # 4. 简日：包含显式的简日特征词；必须排除繁体
    elif value == "简日":
        must_contain = [("简日", False), ("chs_jp", False), ("chs&jpg", False), ("jp_ch", False)]
        must_exclude = traditional_terms
        return must_contain, must_exclude

    # 5. 繁日：包含显式的繁日特征词；必须排除简体
    elif value == "繁日":
        must_contain = [("繁日", False), ("cht_jp", False)]
        must_exclude = simplified_terms
        return must_contain, must_exclude

    # 6. RAW：命中各种无字压制源/原盘特征
    elif value == "RAW":
        must_contain = raw_terms
        must_exclude = []
        return must_contain, must_exclude

    # 7. 日文/无字：包含日语特征；但绝对不能有中文（简/繁）以及 RAW 标志
    elif value == "日文/无字":
        must_contain = japanese_terms
        must_exclude = simplified_terms + traditional_terms + raw_terms
        return must_contain, must_exclude

    # 默认不限
    return [], []


def _release_type_terms(value: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """
    单集/合集筛选 -> (必须包含的候选词, 必须排除的候选词)。
    "合集特征词" 与前端isBatch判断保持一致,包含一个数字范围的正则片段(如01-12)。
    """
    batch_terms: list[tuple[str, bool]] = [
        ("合集", False),
        ("全集", False),
        ("batch", False),
        ("pack", False),
        ("fin", False),
        (r"\d+-\d+", True),
    ]
    if value == "单集(追更)":
        return ([], batch_terms)  # 不要求包含什么,但必须都不命中"合集特征词"
    if value == "合集/全集(完结)":
        return (batch_terms, [])  # 必须命中"合集特征词"里的任意一个
    return ([], [])


def build_must_contain(rule: SubscriptionRule) -> str:
    """
    把关键词/字幕组/画质/字幕语言/格式/单集或合集这几项筛选,
    翻译成一条qBittorrent自动下载规则能用的正则,
    让RSS命中的范围尽量贴近用户在搜索页设置的筛选结果。
    """
    pattern = ""
    # 关键词过滤始终生效,不管是不是"AnimeGarden来源+有bgm_id"这种按subject锁定
    # 单一番剧的场景——subject锁定确实不需要靠关键词去区分"这是不是这部番",
    # 但同一个bgm_id下不同发布批次/字幕组的标题写法可能有实质差异(比如同一部番
    # 第三季,一批标题写"第三季"、另一批写数字"3"),用户在搜索框多打的字
    # (比如"第三季")就是想把这类差异也筛掉,不是单纯的"确认番剧对不对"。
    # 关键词为空(比如没打字,只是从详情页带bgm_id跳过来)时,下面的循环天然不加
    # 任何条件,不影响"按ID全量匹配"这个默认行为。
    # 大小写不敏感——同一部番不同批次/字幕组的标题大小写写法可能不一致(实测
    # 踩到过:同一部番有的集数标题是"Grand Blue",有的集数标题只有全大写的
    # "GRAND BLUE",没有大小写混合写法),按原样大小写敏感匹配会把这些集数
    # 漏掉,导致明明该下载的新种子因为差这一个大小写永远匹配不上。
    keywords = _split_keyword(rule.keyword)
    for kw in keywords:
        if kw.strip():
            pattern += _require_any([(kw.strip(), False)])

    # AnimeGarden建feed时,fansub参数已经在服务器端做过筛选了(跟关键词/subject同理),
    # 不需要也不应该再拿这个规范化的字幕组名字去跟原始标题文本做二次匹配——
    # 标题里印的是字幕组自己的原始展示名(如"黑ネズミたち"),不是AnimeGarden内部的规范名("Kirara Fantasia")。
    # dmhy没有这层服务器端归一化,继续依赖标题文本匹配。
    if rule.fansub_name and rule.source != "animegarden":
        pattern += _require_any([(rule.fansub_name, False)], ignorecase=False)
    if rule.quality:
        pattern += _require_any([(rule.quality, False)])
    if rule.format:
        pattern += _require_any([(rule.format, False)])
    if rule.subtitle:
        pos, neg = _subtitle_terms(rule.subtitle)
        if pos:
            pattern += _require_any(pos)
        if neg:
            pattern += _forbid_any(neg)
    if rule.release_type:
        pos, neg = _release_type_terms(rule.release_type)
        if pos:
            pattern += _require_any(pos)
        if neg:
            pattern += _forbid_any(neg)
    return pattern + ".*"


def build_rss_rule_def(rule: SubscriptionRule, save_path: str) -> dict:
    """组装qBittorrent"自动下载规则"的定义。"""
    return {
        "enabled": True,
        "mustContain": build_must_contain(rule),
        "mustNotContain": "",
        "useRegex": True,
        "episodeFilter": "",
        "smartFilter": False,
        "previouslyMatchedEpisodes": [],
        "affectedFeeds": [rule.rss_url],
        "ignoreDays": 0,
        "lastMatch": "",
        "addPaused": None,
        "assignedCategory": "anime-hub",
        "savePath": save_path,
    }
