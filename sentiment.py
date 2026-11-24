"""文本情绪层：词典、语境修正、persona互斥分类、草根权重、营销过滤。

与采集层、聚合层完全解耦：输入帖子文本，输出分数与诊断。
核心约束：
* 否定前缀反转短语方向（"不会上涨"不得分）
* 切换语境（"卖出A买入B"）：A计负、B计正
* 双向条件计划（"突破就加仓跌破就止损"）≈中性
* persona互斥：家庭词 > 新手词 > 交易词；persona分数必须用遮蔽证据词后的文本
* 词典与全量分数信号分离维护
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Mapping, Tuple

CN_TZ = timezone(timedelta(hours=8))
AUTHOR_DAILY_LIMIT = 3
ESTIMATED_SOURCE_DAY_LIMIT = 8

POSITIVE = {
    "跌不动": 2, "利空出尽": 3, "黄金坑": 3, "涨停": 3, "封板": 3,
    "起飞": 3, "翻倍": 3, "暴涨": 3, "大涨": 3, "普涨": 2,
    "补涨": 2, "牛市": 3, "主升": 3, "新高": 3, "突破": 3,
    "反转": 2, "反包": 3, "大阳线": 2, "走强": 2, "回暖": 1,
    "连板": 3, "大肉": 3, "吃肉": 3, "抄底": 3, "加仓": 3,
    "满仓": 3, "梭哈": 3, "上车": 3, "买入": 3, "看多": 3,
    "利好": 3, "低吸": 3, "龙头": 3, "必涨": 3, "赚钱": 3,
    "发财": 3, "洗盘": 2, "看好": 1, "上涨": 1, "反弹": 1,
    "机会": 1, "强势": 1, "拿住": 1, "格局": 1, "回本": 1,
    "红盘": 1, "稳了": 1, "继续涨": 1, "牛股": 2,
}

NEGATIVE = {
    "看不出来资金在跑路": -3, "看不出资金在跑路": -3,
    "资金正在跑路": -3, "资金在跑路": -3,
    "不要盲目加仓": -2, "别盲目加仓": -2, "卖出当前标的": -3,
    "卖出当前股票": -3, "卖出这只股票": -3, "突破不了": -2,
    "没有龙头": -2, "放量下杀": -3, "放量下跌": -3,
    "资金跑路": -3, "不要加仓": -2, "盲目加仓": -2,
    "不建议追涨": -2, "不要追高": -2, "不能追高": -2, "谨慎追高": -1,
    "避免追高": -2, "关灯吃面": -3, "杀猪盘": -3, "跌停": -3,
    "暴跌": -3, "崩盘": -3, "清仓": -3, "割肉": -3, "爆仓": -3,
    "退市": -3, "完蛋": -3, "血亏": -3, "大面": -3, "跑路": -3,
    "诈骗": -3, "套死": -3, "腰斩": -3, "大跌": -2, "下跌": -2,
    "看空": -2, "利空": -2, "逃命": -2, "砸盘": -2, "出货": -2,
    "套牢": -2, "被套": -2, "亏损": -2, "亏死": -2, "垃圾": -2,
    "骗子": -2, "减仓": -2, "撤退": -2, "风险": -2, "破位": -2,
    "跳水": -2, "阴跌": -2, "熊市": -2, "不涨": -1, "不行": -1,
    "弱势": -1, "难受": -1, "担心": -1, "瑟瑟": -1, "寻底": -1,
    "走下坡": -1, "天天跌": -2, "只会跌": -2, "一直跌": -2,
    "下杀": -2, "跌破": -2, "止损": -2, "卖出": -2,
}

FOMO_WORDS = (
    "梭哈", "满仓", "上车", "追涨", "追高", "踏空", "涨停", "翻倍",
    "起飞", "冲进去", "抢筹", "加仓", "最后机会", "来不及",
    "错过", "龙头", "必涨", "抄底", "低吸", "拿住", "格局", "抢跑",
)
PANIC_WORDS = (
    "资金在跑路", "资金跑路",
    "割肉", "清仓", "跌停", "崩", "完了", "完蛋", "跑路", "逃命",
    "套死", "爆仓", "救命", "血亏", "腰斩", "关灯吃面", "大跌",
    "暴跌", "亏死", "退市", "瀑布", "踩踏",
)
NEWCOMER_WORDS = (
    "小白", "新手", "新人", "求问", "请教", "能买吗", "还能买吗",
    "怎么走", "怎么办", "怎么操作", "如何", "开户", "不懂", "求带",
    "求解", "什么是", "懂的说说", "老师", "第一次", "哪位知道",
    "能不能买", "可以买吗", "目标价", "成本价", "解套", "仓位多少",
    "现在进", "能上车吗", "该卖吗", "该买吗",
)
QUESTION_WORDS = (
    "买", "卖", "仓", "成本", "解套", "进场", "上车", "目标", "走势",
    "操作", "持有", "补仓", "止损", "怎么办", "怎么走",
)
INTENSE_WORDS = (
    "绝对", "肯定", "必须", "完蛋", "必涨", "必跌", "疯了", "天哪",
    "卧槽", "梭哈", "暴涨", "暴跌", "涨停", "跌停", "翻倍", "崩盘",
    "救命", "全线", "普涨", "大涨", "大跌", "爆发",
)
SPAM_WORDS = (
    "加微信", "加薇", "进群", "加群", "电话", "领取资料", "老师带",
    "带你操作", "免费领取", "开户链接", "彩票", "广告合作", "私信领取",
    "扫码", "公众号", "扣666", "扣888", "扣999", "课程报名",
)
MARKETING_WORDS = (
    "点赞关注", "点赞", "关注我", "关注", "盘中操作", "今日上车",
    "带大家", "挑战翻倍", "粉丝", "直播", "老师", "私信",
)
MARKETING_CALLS = ("点赞关注", "关注我", "盘中操作", "今日上车", "带大家", "私信", "评论区")
SKEPTICAL_MARKERS = ("就是个名", "徒有虚名", "名不副实", "所谓", "呵呵", "就这")
UNCERTAINTY_MARKERS = ("看不懂", "不明白", "怎么理解", "什么意思", "说不清")
AUTHOR_BLOCK_WORDS = (
    "官方", "基金", "ETF", "证券", "投顾", "首席", "研究", "研报",
    "财经", "情报", "机构", "数据宝", "交易所", "智库", "媒体",
    "荐股", "带单", "财富号",
)
LOW_INFORMATION = {
    "转发", "签到", "打卡", "路过", "哈哈", "呵呵", "早上好", "早安",
    "晚安", "谢谢", "收到", "支持", "牛", "赞", "666", "888",
}
NEGATION_WORDS = ("不", "没", "没有", "别", "莫", "勿", "避免", "不能", "不要", "谨慎")
NEGATION_SUFFIXES = ("不了", "不成", "失败", "无望")
CAUTION_PREFIXES = ("盲目", "冲动", "随便")

# persona证据词：互斥优先级 家庭 > 新手 > 交易。宝妈仅指家庭稳健型表达，
# 不推断作者性别、年龄或真实身份。
HOUSEHOLD_EXPLICIT_RE = re.compile(
    r"宝妈|宝宝|孩子|小孩|奶粉|学费|家庭|家里|生活费|买菜|房贷|车贷|"
    r"老公|老婆|母婴|养老|退休|工资|存款|闲钱|攒钱"
)
HOUSEHOLD_CAUTIOUS_RE = re.compile(
    r"稳健|长期持有|长线|分红|股息|定投|价值投资|拿着不动|不折腾|"
    r"低风险|保本|每个月|每月投入|闲钱投资|养老钱"
)
NOVICE_EVIDENCE_RE = re.compile(
    r"新手|小白|第一次|刚入市|刚开户|不懂|求教|请教|请问|老师|大神|"
    r"怎么|如何|为什么|为何|怎么办|能买吗|能不能买|该不该|要不要|"
    r"多少买|怎么看|啥意思|什么意思|解套吗|补仓吗|卖吗"
)
NOVICE_MARKET_CONTEXT_RE = re.compile(
    r"股票|个股|板块|仓位|成本|均线|分红|除权|涨停|跌停|买|卖|补仓|解套"
)
RETAIL_ACTION_RE = re.compile(
    r"买入|卖出|加仓|减仓|补仓|清仓|满仓|半仓|持仓|仓位|成本|做T|"
    r"止损|止盈|抄底|追涨|套牢|解套|看多|看空|突破|支撑|压力|量能|"
    r"均线|主力|庄家|洗盘|出货|筹码|技术面|基本面|龙虎榜|集合竞价|"
    r"涨停|跌停|龙头|资金流|换仓"
)
GRASSROOTS_VOICE_RE = re.compile(
    r"我|咱|兄弟|老师|求问|请教|怎么办|能买吗|能卖吗|该买吗|该卖吗|"
    r"套了|被套|成本|回本|解套|求安慰|梭哈|满仓|加仓|补仓|减仓|"
    r"卖飞|踏空|割肉|拿不住|上车|下车|明天|今天|救命|慌|难受|"
    r"第一次|小白|新手|看不懂|不懂"
)
PROFESSIONAL_STYLE_RE = re.compile(
    r"复盘|投研|研究框架|产业链|估值|基本面|研报|投资策略|策略报告|"
    r"主线梳理|逻辑梳理|不构成投资建议|盘前策略|盘后策略|机构观点|"
    r"核心标的|建议关注|免责声明"
)
PERSONA_EVIDENCE_KEYS = ("baoma", "novice", "retail")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def round1(value: float) -> float:
    return round(clamp(value), 1)


def safe_int(value: object, default: int = 0) -> int:
    try:
        raw = default if value is None or str(value).strip() == "" else value
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return default


def clean_text(value: object, max_chars: int = 260) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<hx_stock[^>]*>\s*stockName:([^,<]+).*?</hx_stock>", r" \1 ", text, flags=re.I | re.S)
    text = re.sub(r"<hx_at[^>]*>.*?</hx_at>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].rstrip()


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text).lower()


def valid_text(text: str) -> bool:
    compact = normalize_text(text)
    if len(compact) < 4 or compact in LOW_INFORMATION:
        return False
    if any(word in text for word in SPAM_WORDS):
        return False
    marketing_hits = sum(1 for word in MARKETING_WORDS if word in text)
    if len(compact) >= 30 and marketing_hits >= 2 and any(word in text for word in MARKETING_CALLS):
        return False
    return True


def grassroots_style_weight(post: Mapping) -> float:
    """只判断表达形态，不推断身份：普通评论/口语提问满权，长投研腔、多股推广对称降权。"""
    if bool(post.get("certified")):
        return 0.0
    text = str(post.get("title") or "")
    compact_length = max(len(normalize_text(text)), safe_int(post.get("originalLength")))
    kind = str(post.get("kind") or "post")
    if kind in {"comment", "reply"}:
        weight = 1.0
    elif compact_length <= 240:
        weight = 1.0
    elif compact_length <= 600:
        weight = 0.45
    else:
        weight = 0.10
    stock_tags = max(0, safe_int(post.get("stockTagCount")))
    if stock_tags >= 3:
        weight *= 0.35
    elif stock_tags == 2:
        weight *= 0.70
    professional_hits = len(PROFESSIONAL_STYLE_RE.findall(text))
    if professional_hits:
        weight *= max(0.20, 1.0 - 0.30 * professional_hits)
    if GRASSROOTS_VOICE_RE.search(text) or re.search(r"[?？]", text):
        weight *= 1.15
    return round(max(0.0, min(1.0, weight)), 4)


def parse_time(value: object):
    if isinstance(value, (int, float)) or str(value or "").strip().isdigit():
        number = float(value or 0)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, timezone.utc).astimezone(CN_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def stable_estimated_post_key(post: Mapping) -> bytes:
    """与情绪标签无关的历史稳定抽样键：sha256(sourceKey,id,publishTime,authorKey)。"""
    post_id = str(post.get("id") or "")
    fallback = "" if post_id else normalize_text(str(post.get("title") or ""))
    identity = "|".join((
        str(post.get("sourceKey") or "unknown"),
        post_id,
        str(post.get("publishTime") or post.get("published") or ""),
        str(post.get("authorKey") or post.get("authorHash") or ""),
        fallback,
    ))
    return hashlib.sha256(identity.encode("utf-8")).digest()


def has_negating_prefix(prefix: str) -> bool:
    """只在当前分句紧邻前缀识别否定，避免跨逗号误反转下一分句。"""
    clause = re.split(r"[，,。！？!?；;：:]", prefix)[-1]
    nearby = clause[-5:]
    if nearby.endswith(("看不出", "没看出", "看不出来", "没看出来")):
        return False
    return any(word in nearby for word in NEGATION_WORDS)


def _topic_terms(topic: object = None, source: object = None):
    values = [topic]
    if isinstance(source, Mapping):
        values.extend((source.get("name"), source.get("source"), source.get("topic")))
    else:
        values.append(source)
    generic = {"公开社区", "东方财富股吧", "新浪股吧", "同花顺论股堂", "股吧", "行业吧"}
    terms = []
    for value in values:
        term = str(value or "").strip()
        for suffix in ("东方财富股吧", "新浪股吧", "同花顺论股堂", "行业吧", "股吧", "吧"):
            if term.endswith(suffix):
                term = term[:-len(suffix)].strip()
                break
        if len(term) >= 2 and term not in generic and term not in terms:
            terms.append(term)
            for company_suffix in ("股份有限公司", "股份", "集团"):
                if term.endswith(company_suffix) and len(term) - len(company_suffix) >= 2:
                    alias = term[:-len(company_suffix)]
                    if alias not in terms:
                        terms.append(alias)
    return terms


def trade_switch_adjustment(text: str, topic: object = None, source: object = None) -> int:
    """切换语境：卖出当前标的换别的 → 负；换入当前标的 → 正。"""
    compact = re.sub(r"\s+", "", str(text or ""))
    generic_away = re.search(
        r"(?:卖出|清仓|减仓)(?:当前|这只|本股|手中|持有的)?(?:标的|股票|个股|票)?"
        r".{0,10}(?:买入|换入)(?:别的|其他|其它|另一只|另一个)",
        compact,
    )
    if generic_away:
        return -3
    for term in sorted(_topic_terms(topic, source), key=len, reverse=True):
        escaped = re.escape(term)
        sell = re.search(rf"(?:卖出|清仓|减仓).{{0,8}}{escaped}", compact)
        buy = re.search(rf"(?:买入|加仓|换入).{{0,8}}{escaped}", compact)
        if sell and re.search(r"(?:买入|换入)", compact[sell.end():]):
            return -3
        if buy and re.search(r"(?:卖出|清仓|减仓)", compact[:buy.start()]):
            return 2
    return 0


def dual_directional_condition(text: str) -> bool:
    """双向条件计划（突破就买、跌破就卖）≈中性。"""
    compact = re.sub(r"\s+", "", str(text or ""))
    bullish = re.search(r"(?:突破|站上|上破|上涨).{0,12}(?:买入|加仓|持有|看多|做多)", compact)
    bearish = re.search(r"(?:跌破|破位|下跌).{0,12}(?:卖出|减仓|清仓|止损|看空)", compact)
    bearish_scenario = re.search(r"(?:如果|假如|若|要是).{0,12}(?:下探|回落|跌破|破位|下跌)", compact)
    return bool(
        bullish and (bearish or bearish_scenario)
        and re.search(r"如果|假如|若|要是|就|则|否则", compact)
    )


def sentiment_score(text: str, topic: object = None, source: object = None) -> int:
    """短语情绪分，每帖每短语最多一次。否定前缀反转；正负证据并存收缩0.8；双向条件×0.2。"""
    working = str(text or "")
    positive_total = 0.0
    negative_total = 0.0
    entries = sorted([*POSITIVE.items(), *NEGATIVE.items()], key=lambda item: len(item[0]), reverse=True)
    for phrase, base_weight in entries:
        index = working.find(phrase)
        if index < 0:
            continue
        prefix = working[max(0, index - 5):index]
        suffix = working[index + len(phrase):index + len(phrase) + 9]
        weight = float(base_weight)
        if has_negating_prefix(prefix):
            weight = -weight
        elif weight > 0 and (
            any(suffix.startswith(marker) for marker in NEGATION_SUFFIXES)
            or any(marker in prefix for marker in CAUTION_PREFIXES)
        ):
            weight = -abs(weight)
        elif weight > 0 and any(marker in suffix or marker in prefix for marker in SKEPTICAL_MARKERS):
            weight = -abs(weight)
        if weight > 0:
            positive_total += weight
        elif weight < 0:
            negative_total += weight
        working = working.replace(phrase, " ")

    switch = trade_switch_adjustment(text, topic=topic, source=source)
    if switch > 0:
        positive_total += switch
    elif switch < 0:
        negative_total += switch
    total = positive_total + negative_total
    if positive_total and negative_total:
        total *= 0.80
    if dual_directional_condition(text):
        total *= 0.20
    if any(marker in text for marker in UNCERTAINTY_MARKERS):
        total *= 0.40
    return max(-6, min(6, int(round(total))))


def contains_any(text: str, words) -> bool:
    return any(word in text for word in words)


def contains_nonnegated(text: str, words) -> bool:
    for word in words:
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            prefix = text[max(0, index - 5):index]
            suffix = text[index + len(word):index + len(word) + 9]
            if (
                not has_negating_prefix(prefix)
                and not any(suffix.startswith(marker) for marker in NEGATION_SUFFIXES)
                and not any(marker in prefix for marker in CAUTION_PREFIXES)
                and not any(marker in suffix for marker in SKEPTICAL_MARKERS)
            ):
                return True
            start = index + len(word)
    return False


def newcomer_flag(text: str) -> bool:
    if contains_any(text, NEWCOMER_WORDS):
        return True
    return bool(re.search(r"[?？]", text) and contains_any(text, QUESTION_WORDS))


def signal_from_rate(rate: float, kind: str) -> float:
    if kind == "fomo":
        return round1(50.0 + 30.0 * math.tanh((rate - 0.06) / 0.07))
    if kind == "newcomer":
        return round1(50.0 + 28.0 * math.tanh((rate - 0.02) / 0.035))
    if kind == "intensity":
        return round1(50.0 + 25.0 * math.tanh((rate - 0.18) / 0.16))
    raise ValueError(kind)


def classify_persona_evidence(text: str) -> Tuple:
    """互斥persona分类 + 遮蔽证据词后的评分文本（防自证循环）。"""
    cleaned = str(text or "").strip()
    label = None
    reason = None
    patterns = ()
    if HOUSEHOLD_EXPLICIT_RE.search(cleaned):
        label, reason = "baoma", "explicit_household"
        patterns = (HOUSEHOLD_EXPLICIT_RE, HOUSEHOLD_CAUTIOUS_RE)
    elif HOUSEHOLD_CAUTIOUS_RE.search(cleaned):
        label, reason = "baoma", "cautious_household_style"
        patterns = (HOUSEHOLD_EXPLICIT_RE, HOUSEHOLD_CAUTIOUS_RE)
    elif NOVICE_EVIDENCE_RE.search(cleaned) or (
        re.search(r"[?？]", cleaned) and NOVICE_MARKET_CONTEXT_RE.search(cleaned)
    ):
        label = "novice"
        reason = "explicit_question_or_learning" if NOVICE_EVIDENCE_RE.search(cleaned) else "market_question"
        patterns = (NOVICE_EVIDENCE_RE,)
    elif RETAIL_ACTION_RE.search(cleaned):
        label, reason = "retail", "active_trading_language"
        patterns = (RETAIL_ACTION_RE,)
    masked = cleaned
    for pattern in patterns:
        masked = pattern.sub(" ", masked)
    masked = re.sub(r"\s+", " ", masked).strip(" ，。！？!?：:；;")
    return label, reason, masked
