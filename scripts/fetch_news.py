#!/usr/bin/env python3
"""Fetch retail intelligence news from public RSS sources.

The script intentionally uses Python standard library only. It can run with or
without DEEPSEEK_API_KEY. Existing docs/news.json is merged, not replaced.
"""

from __future__ import annotations

import email.utils
import difflib
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
WATCHLIST_PATH = ROOT / "config" / "watchlist.json"
TAXONOMY_PATH = ROOT / "config" / "taxonomy.json"
NEWS_PATH = ROOT / "docs" / "news.json"

VERSION = "1.0.0"
LOCAL_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (compatible; RetailNewsMonitor/1.0; "
    "+https://github.com/retail-news-monitor)"
)

MAX_ITEMS_PER_QUERY = int(os.getenv("RETAIL_NEWS_MAX_ITEMS_PER_QUERY", "10"))
MAX_QUERIES_PER_ENTITY = int(os.getenv("RETAIL_NEWS_MAX_QUERIES_PER_ENTITY", "12"))
MAX_HISTORY_ITEMS = int(os.getenv("RETAIL_NEWS_MAX_HISTORY_ITEMS", "1500"))
REQUEST_TIMEOUT = int(os.getenv("RETAIL_NEWS_REQUEST_TIMEOUT", "12"))
REQUEST_DELAY_SECONDS = float(os.getenv("RETAIL_NEWS_REQUEST_DELAY_SECONDS", "0.08"))
QUERY_WINDOW = os.getenv("RETAIL_NEWS_QUERY_WINDOW", "when:30d").strip()
REANALYZE_EXISTING = os.getenv("REANALYZE_EXISTING", "false").strip().lower() in ("1", "true", "yes", "y")
RESET_NEWS = os.getenv("RESET_NEWS", "false").strip().lower() in ("1", "true", "yes", "y")
ARTICLE_FETCH_LIMIT = int(os.getenv("ARTICLE_FETCH_LIMIT", "120"))
ARTICLE_REQUEST_TIMEOUT = int(os.getenv("ARTICLE_REQUEST_TIMEOUT", "10"))
ARTICLE_READ_BYTES = int(os.getenv("ARTICLE_READ_BYTES", str(1024 * 1024)))

SECTION_QUERY_BUDGETS = {
    "platform": int(os.getenv("RETAIL_NEWS_PLATFORM_QUERY_BUDGET", "80")),
    "retailer": int(os.getenv("RETAIL_NEWS_RETAILER_QUERY_BUDGET", "150")),
    "category": int(os.getenv("RETAIL_NEWS_CATEGORY_QUERY_BUDGET", "220")),
}
SECTION_SOURCE_QUERY_BUDGETS = {
    "platform": int(os.getenv("RETAIL_NEWS_PLATFORM_SOURCE_QUERY_BUDGET", "24")),
    "retailer": int(os.getenv("RETAIL_NEWS_RETAILER_SOURCE_QUERY_BUDGET", "36")),
    "category": int(os.getenv("RETAIL_NEWS_CATEGORY_SOURCE_QUERY_BUDGET", "48")),
}
CATEGORY_MIN_QUERIES_PER_MONITOR = int(os.getenv("RETAIL_NEWS_CATEGORY_MIN_QUERIES_PER_MONITOR", "12"))
CATEGORY_MAX_QUERIES_PER_MONITOR = int(os.getenv("RETAIL_NEWS_CATEGORY_MAX_QUERIES_PER_MONITOR", "28"))

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").strip() or "deepseek"
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip() or os.getenv("DEEPSEEK_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
if not LLM_BASE_URL and LLM_PROVIDER.lower() == "deepseek":
    LLM_BASE_URL = "https://api.deepseek.com"
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "").strip() or f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
LLM_MODEL = os.getenv("LLM_MODEL", "").strip() or os.getenv("DEEPSEEK_MODEL", "").strip() or (
    "deepseek-v4-flash" if LLM_PROVIDER.lower() == "deepseek" else ""
)
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", os.getenv("DEEPSEEK_MAX_TOKENS", "5000")))
DEEPSEEK_MAX_NEWS_PER_RUN = int(os.getenv("DEEPSEEK_MAX_NEWS_PER_RUN", "200"))
DEEPSEEK_BATCH_SIZE = int(os.getenv("DEEPSEEK_BATCH_SIZE", "20"))


RULE_CATEGORY_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    ("financial", ("财报", "业绩", "营收", "利润", "GMV", "earnings", "revenue", "profit", "sales")),
    ("organization", ("组织", "架构", "任命", "高管", "CEO", "CFO", "调整", "人事")),
    ("policy", ("政策", "规则", "监管", "合规", "标准", "处罚", "食品安全", "召回")),
    ("store", ("开店", "拓店", "新店", "门店", "闭店", "关店", "store opening", "new store")),
    ("member", ("会员", "用户", "复购", "客单价", "membership", "member")),
    ("private_label", ("自有品牌", "private label", "Kirkland", "Member's Mark", "自牌")),
    ("hot_product", ("爆品", "热卖", "爆款", "明星商品")),
    ("price", ("价格", "低价", "折扣", "补贴", "百亿补贴", "促销", "price", "discount")),
    ("supply", ("供应链", "仓储", "物流", "履约", "前置仓", "supply chain", "warehouse", "fulfillment")),
    ("instant", ("即时零售", "闪购", "买药", "小时达", "到家", "instant retail")),
    ("new_product", ("新品", "发布", "上新", "联名", "限定", "new product", "launch")),
    ("promotion", ("大促", "618", "双11", "双十二", "年货节")),
    ("content", ("直播", "达人", "自播", "短视频", "内容电商", "live commerce")),
    ("marketing", ("营销", "活动", "品牌传播", "种草", "广告投放")),
    ("channel", ("渠道", "铺货", "经销商", "商家合作", "平台合作")),
    ("ai", ("AI", "人工智能", "大模型", "千问", "通义", "Qwen", "豆包", "Doubao")),
    ("ma", ("并购", "收购", "合并", "投资", "合作", "merger", "acquisition", "partnership")),
]

INSIGHT_TYPES = ("机会", "预警", "动作", "关注")

BUSINESS_HOT_KEYWORDS = [
    "平台政策", "组织调整", "财报业绩", "低价策略", "价格力", "百亿补贴",
    "即时零售", "内容电商", "直播电商", "商家生态", "会员体系", "自有品牌",
    "爆品", "供应链", "履约成本", "前置仓", "开店拓店", "门店调改",
    "硬折扣", "精选SKU", "食品安全", "监管合规", "行业政策", "新品趋势",
    "渠道动作", "营销活动", "无糖茶", "高蛋白", "宠物主粮", "湿厕纸",
    "洗衣凝珠", "低GI", "零添加", "功能饮料", "儿童用品", "功效宣称",
    "IP联名", "AI玩具", "量贩零食", "功能粮", "GMV", "SKU", "AI",
]

CATEGORY_HOT_KEYWORDS = {
    "业务规模 & GMV表现": ["GMV", "业务表现", "增长质量"],
    "财报业绩": ["财报业绩", "营收利润", "利润承压"],
    "组织架构": ["组织调整", "组织效率"],
    "组织调整": ["组织调整", "组织效率"],
    "平台政策": ["平台政策", "商家生态", "经营规则"],
    "行业政策": ["行业政策", "监管合规"],
    "监管合规": ["监管合规", "平台合规"],
    "食品安全": ["食品安全", "监管合规"],
    "价格力策略": ["价格力", "低价策略"],
    "价格变化": ["价格力", "价格波动"],
    "自有品牌": ["自有品牌", "差异化货盘"],
    "会员 / 用户": ["会员体系", "用户留存"],
    "供应链能力": ["供应链", "履约成本"],
    "即时零售": ["即时零售", "履约成本"],
    "新品发布": ["新品趋势", "爆品"],
    "行业热点 / 新品趋势": ["新品趋势", "趋势货盘"],
    "重点品牌动态": ["重点品牌", "渠道动作"],
    "渠道动作": ["渠道动作", "品牌资源"],
    "营销活动": ["营销活动", "用户转化"],
    "内容电商打法": ["内容电商", "直播电商"],
    "开店 / 拓店": ["开店拓店", "区域竞争"],
    "并购合作": ["并购合作", "资源整合"],
    "科技 / AI": ["科技AI", "运营效率"],
    "爆品": ["爆品", "高复购"],
    "大促专栏": ["大促策略", "促销强度"],
}

HOT_KEYWORD_BLOCKLIST = (
    "建议", "关注", "该动态", "影响", "当前", "是否", "其对", "对京东",
    "从经营", "表示", "通过", "可能", "相关", "变化", "节奏", "方面",
    "值得", "需要", "持续", "系统", "归类",
)

FACT_EXCERPT_BLOCKLIST = (
    "建议关注", "从经营情报看", "对京东商超而言", "该动态涉及",
    "需持续关注", "当前重要性", "按规则归类", "建议结合命中关键词",
)


def warn(message: str) -> None:
    print(f"[warning] {message}", file=sys.stderr)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as exc:  # pragma: no cover - defensive for Actions safety.
        warn(f"Failed to read {path}: {exc}")
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        with path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        return

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    try:
        tmp_path.replace(path)
    except PermissionError as exc:
        warn(f"Atomic replace failed for {path}; using direct write fallback: {exc}")
        with path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        try:
            tmp_path.unlink()
        except Exception:
            pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def text_signal_length(value: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", value or ""))
    latin = len(re.findall(r"[A-Za-z0-9]", value or ""))
    return cjk + latin // 4


def is_too_similar(a: str, b: str, threshold: float = 0.86) -> bool:
    left = normalize_title(a)
    right = normalize_title(b)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 12 and shorter in longer:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= threshold


def is_valid_excerpt(excerpt: str, title: str = "") -> bool:
    excerpt = clean_text(excerpt)
    if not excerpt:
        return False
    if any(phrase in excerpt for phrase in FACT_EXCERPT_BLOCKLIST):
        return False
    if text_signal_length(excerpt) < 20:
        return False
    if title and is_too_similar(excerpt, title):
        return False
    if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9 .·'’_-]{2,24}", excerpt):
        return False
    return True


def clean_description(description: str, title: str = "") -> str:
    raw = html.unescape(description or "")
    raw = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "。", raw)
    raw = re.sub(r"(?is)<font\b[^>]*>.*?</font>", " ", raw)
    raw = re.sub(r"(?is)<a\b[^>]*>.*?</a>", " ", raw)
    text = clean_text(raw)
    text = re.sub(r"\bFull Coverage\b|查看完整报道|阅读原文|更多报道|原文链接", " ", text, flags=re.I)
    text = re.sub(r"\s*[-_—–|｜]\s*(Google News|谷歌新闻|百度新闻|腾讯新闻|新浪财经|界面新闻|36氪|虎嗅网|亿邦动力|联商网)\s*$", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -_—–|｜。")

    clean_title = clean_text(title)
    if clean_title and text.startswith(clean_title):
        text = text[len(clean_title):].strip(" -_—–|｜。")
    if clean_title and text.endswith(clean_title):
        text = text[:-len(clean_title)].strip(" -_—–|｜。")
    for phrase in FACT_EXCERPT_BLOCKLIST:
        text = text.replace(phrase, "")
    text = re.sub(r"\s+", " ", text).strip(" -_—–|｜。")
    return text[:520]


def clean_source_excerpt(description: str, title: str = "") -> str:
    """Clean RSS description while keeping source facts instead of AI analysis."""
    text = clean_description(description, title)
    return text if is_valid_excerpt(text, title) else ""


def normalize_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"\s[-_|·:：]\s[^-_|·:：]{2,30}$", "", title)
    title = re.sub(r"[^\w\u4e00-\u9fff]+", "", title, flags=re.UNICODE)
    return title.strip()


def stable_id(title: str, link: str) -> str:
    seed = link.strip().lower() or normalize_title(title)
    if not seed:
        seed = title
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def dedupe_preserve(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = re.sub(r"\s+", " ", value).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def split_sentences(text: str) -> List[str]:
    text = clean_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?])\s+|[。！？!?]\s*", text)
    return [part.strip(" ，,；;") for part in parts if part.strip(" ，,；;")]


def join_sentences(sentences: Iterable[str], limit: int = 5) -> str:
    clean_sentences = []
    for sentence in sentences:
        sentence = clean_text(sentence).strip("。")
        if sentence and sentence not in clean_sentences:
            clean_sentences.append(sentence)
        if len(clean_sentences) >= limit:
            break
    return "。".join(clean_sentences) + ("。" if clean_sentences else "")


def is_englishish(value: str) -> bool:
    letters = re.findall(r"[A-Za-z]", value or "")
    return len(letters) >= 3


def has_deepseek_api_key() -> bool:
    return has_llm_api_key()


def has_llm_api_key() -> bool:
    return bool(LLM_API_KEY and LLM_ENDPOINT and LLM_MODEL)


def ai_provider_name() -> str:
    return LLM_PROVIDER.lower() if has_llm_api_key() else "rule_based"


def item_is_recent(item: Dict[str, Any], days: int = 7) -> bool:
    published = parse_datetime(item.get("published", ""))
    if not published:
        return False
    return published >= utc_now() - timedelta(days=days)


def is_mostly_english(text: str) -> bool:
    """Return true for titles that are effectively English-only."""
    text = clean_text(text)
    letters = len(re.findall(r"[A-Za-z]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    digits = len(re.findall(r"\d", text))
    signal = letters + cjk + digits
    if signal == 0:
        return False
    if cjk == 0 and letters >= 8:
        return True
    return letters >= 16 and letters / max(signal, 1) >= 0.72 and letters > cjk * 3


def keyword_in_text(keyword: str, text: str) -> bool:
    if not keyword:
        return False
    if is_englishish(keyword):
        return keyword.lower() in text.lower()
    return keyword in text


def is_good_hot_keyword(value: str) -> bool:
    word = clean_text(value).strip(" ，,；;。.!！?？、")
    if not word:
        return False
    if any(blocked in word for blocked in HOT_KEYWORD_BLOCKLIST):
        return False
    if len(word) > 10 and word not in BUSINESS_HOT_KEYWORDS:
        return False
    if re.search(r"[\u4e00-\u9fff]", word):
        cjk_len = len(re.findall(r"[\u4e00-\u9fff]", word))
        return 2 <= cjk_len <= 8 or word in BUSINESS_HOT_KEYWORDS
    return word in ("GMV", "SKU", "AI", "IP")


def clean_hot_keywords(values: Iterable[str], limit: int = 6) -> List[str]:
    if isinstance(values, str):
        values = re.split(r"[、,，;；\s]+", values)
    cleaned: List[str] = []
    for value in values:
        word = clean_text(str(value or "")).strip(" ，,；;。.!！?？、")
        word = word.replace("科技 / AI", "科技AI")
        word = word.replace("开店 / 拓店", "开店拓店")
        if not is_good_hot_keyword(word):
            continue
        if word not in cleaned:
            cleaned.append(word)
        if len(cleaned) >= limit:
            break
    return cleaned


def rule_based_hot_keywords(
    category: str,
    subcategory: str,
    title: str,
    summary: str,
    matched_keywords: List[str],
) -> List[str]:
    text = f"{category} {subcategory} {title} {summary} {' '.join(matched_keywords)}"
    candidates: List[str] = []
    candidates.extend(CATEGORY_HOT_KEYWORDS.get(subcategory, []))
    candidates.extend(CATEGORY_HOT_KEYWORDS.get(category, []))
    for word in BUSINESS_HOT_KEYWORDS:
        if keyword_in_text(word, text):
            candidates.append(word)
    for word in matched_keywords:
        if word in BUSINESS_HOT_KEYWORDS or word in CATEGORY_HOT_KEYWORDS.get(category, []):
            candidates.append(word)
    return clean_hot_keywords(candidates, limit=6)


def collect_monitor_keywords(monitor: Dict[str, Any]) -> List[str]:
    fields: List[str] = [
        monitor.get("name", ""),
        monitor.get("display_name", ""),
    ]
    for key in (
        "aliases",
        "dimensions",
        "policy_keywords",
        "trend_keywords",
        "brands",
        "brand_dynamic_keywords",
    ):
        fields.extend(monitor.get(key, []) or [])
    return dedupe_preserve(fields)


def matched_keywords_for(monitor: Dict[str, Any], title: str, summary: str) -> List[str]:
    text = f"{title} {summary}"
    matches = [kw for kw in collect_monitor_keywords(monitor) if keyword_in_text(kw, text)]
    return matches[:20]


def infer_category(section: str, text: str) -> str:
    lowered = text.lower()
    matched_types = []
    for rule_type, words in RULE_CATEGORY_PATTERNS:
        if any(keyword_in_text(word, lowered) for word in words):
            matched_types.append(rule_type)

    if "financial" in matched_types:
        return "业务规模 & GMV表现" if section == "platform" else "财报业绩"
    if "organization" in matched_types:
        return "组织架构" if section == "platform" else "组织调整"
    if "policy" in matched_types:
        if section == "platform":
            return "平台政策"
        if section == "category":
            return "行业政策"
        return "监管合规"
    if "store" in matched_types:
        return "开店 / 拓店"
    if "member" in matched_types:
        return "会员 / 用户"
    if "private_label" in matched_types:
        return "自有品牌"
    if "hot_product" in matched_types:
        return "爆品"
    if "price" in matched_types:
        return "价格力策略" if section == "platform" else "价格变化"
    if "instant" in matched_types:
        return "即时零售"
    if "supply" in matched_types:
        return "供应链能力"
    if "new_product" in matched_types:
        return "行业热点 / 新品趋势" if section == "category" else "新品发布"
    if "promotion" in matched_types:
        return "大促专栏"
    if "content" in matched_types:
        return "内容电商打法"
    if "marketing" in matched_types:
        return "营销活动"
    if "channel" in matched_types:
        return "渠道动作"
    if "ai" in matched_types:
        return "科技 / AI"
    if "ma" in matched_types:
        return "并购合作"
    return "其他"


def infer_category_subcategory(monitor: Dict[str, Any], text: str, category: str) -> str:
    if monitor.get("section") != "category":
        return ""
    policy_words = monitor.get("policy_keywords", []) or []
    trend_words = monitor.get("trend_keywords", []) or []
    brand_words = monitor.get("brands", []) or []
    if category == "行业政策" or any(keyword_in_text(word, text) for word in policy_words):
        return "行业政策"
    if any(keyword_in_text(word, text) for word in brand_words):
        return "重点品牌动态"
    if category in ("行业热点 / 新品趋势", "新品发布") or any(keyword_in_text(word, text) for word in trend_words):
        return "行业热点 / 新品趋势"
    return "全部"


def infer_entity(monitor: Dict[str, Any], text: str) -> Tuple[str, str]:
    if monitor.get("section") == "category":
        for brand in monitor.get("brands", []) or []:
            if keyword_in_text(brand, text):
                return brand, "brand"
        return monitor.get("display_name") or monitor.get("name", ""), "category"
    return monitor.get("display_name") or monitor.get("name", ""), monitor.get("entity_type", "")


def infer_importance(text: str, taxonomy: Dict[str, Any], category: str) -> int:
    importance_keywords = taxonomy.get("importance_keywords", {})
    for score in ("5", "4", "3"):
        if any(keyword_in_text(word, text) for word in importance_keywords.get(score, [])):
            return int(score)
    if category in ("组织架构", "组织调整", "财报业绩", "行业政策", "监管合规", "并购合作", "食品安全"):
        return 5
    if category in ("价格力策略", "自有品牌", "会员 / 用户", "供应链能力", "即时零售", "平台政策"):
        return 4
    if category in ("新品发布", "营销活动", "渠道动作", "行业热点 / 新品趋势"):
        return 3
    if any(keyword_in_text(word, text) for word in importance_keywords.get("2", [])):
        return 2
    return 2 if category != "其他" else 1


def rule_based_structured_insight(
    section: str,
    entity: str,
    category: str,
    subcategory: str,
    title: str,
    summary: str,
    matched_keywords: List[str],
    importance: int,
) -> Dict[str, Any]:
    entity = entity or "相关主体"
    label = subcategory if subcategory and subcategory != "全部" else category
    hot_keywords = rule_based_hot_keywords(category, subcategory, title, summary, matched_keywords)

    if category in ("价格力策略", "价格变化"):
        result = {
            "insight_type": "预警",
            "insight_motive": f"{entity}强化价格动作，通常对应流量争夺、转化效率压力和用户低价心智竞争。",
            "insight_impact": "低价信号会放大用户比价行为，并压缩同类核心 SKU 的毛利空间。",
            "insight_jd_action": "京东商超可对比核心 SKU 价格带、促销强度和履约体验，判断是否强化低价爆品池。",
        }
    elif category == "自有品牌":
        result = {
            "insight_type": "机会",
            "insight_motive": f"{entity}强化自有品牌，多数是为了提升差异化供给和毛利控制能力。",
            "insight_impact": "自有品牌扩张会提高用户对独家商品、会员专属商品和稳定品质的期待。",
            "insight_jd_action": "京东商超可梳理大包装、家庭囤货和高复购品类的自营定制机会。",
        }
    elif category in ("业务规模 & GMV表现", "财报业绩"):
        result = {
            "insight_type": "关注" if importance < 5 else "预警",
            "insight_motive": f"{entity}披露经营表现，背后通常反映增长、利润和投入强度的取舍。",
            "insight_impact": "财务信号会影响竞对补贴力度、商家资源投放和品类扩张节奏。",
            "insight_jd_action": "京东商超可跟踪其核心品类投入、价格补贴和履约成本是否同步变化。",
        }
    elif category in ("组织架构", "组织调整"):
        result = {
            "insight_type": "预警",
            "insight_motive": f"{entity}出现组织变化，通常指向业务优先级、协同效率或增长压力的重新分配。",
            "insight_impact": "组织调整可能带来资源重排，并改变平台政策、品类打法或区域扩张速度。",
            "insight_jd_action": "京东商超可观察后续资源是否转向即时零售、低价、会员或重点品类。",
        }
    elif category in ("平台政策", "监管合规", "行业政策", "食品安全"):
        result = {
            "insight_type": "预警",
            "insight_motive": f"{label}信号通常来自监管要求、平台治理或行业准入标准变化。",
            "insight_impact": "规则收紧会影响商家准入、营销表达、商品资质和供应链履约标准。",
            "insight_jd_action": "京东商超可同步核查品类合规、商家资质和重点商品页面表达，降低经营风险。",
        }
    elif category == "会员 / 用户":
        result = {
            "insight_type": "机会",
            "insight_motive": f"{entity}推进会员或用户经营，通常是为了提高复购、客单价和长期留存。",
            "insight_impact": "会员权益和差异化货盘会抬高用户对商超渠道稳定服务的期待。",
            "insight_jd_action": "京东商超可验证会员专属价、囤货权益和高频品类组合是否提升复购。",
        }
    elif category in ("供应链能力", "即时零售"):
        result = {
            "insight_type": "动作",
            "insight_motive": f"{entity}强化履约或供应链，多数指向时效体验、库存周转和区域覆盖能力。",
            "insight_impact": "履约能力提升会改变用户对到家速度、缺货率和服务稳定性的比较标准。",
            "insight_jd_action": "京东商超可对照小时达、前置仓、仓配协同和履约成本，识别需要补强的场景。",
        }
    elif category in ("新品发布", "行业热点 / 新品趋势", "重点品牌动态", "爆品"):
        result = {
            "insight_type": "机会",
            "insight_motive": f"{entity}出现{label}信号，背后可能是健康化、功能化、场景化或渠道化需求变化。",
            "insight_impact": "新品和爆品动作会影响品牌预算、渠道首发资源和用户尝鲜路径。",
            "insight_jd_action": "京东商超可筛选可复制的趋势货盘，并验证新品首发、试用和爆品孵化资源。",
        }
    elif category in ("渠道动作", "营销活动", "内容电商打法"):
        result = {
            "insight_type": "动作",
            "insight_motive": f"{entity}加强渠道或营销动作，通常指向流量获取、品牌触达和货架转化效率。",
            "insight_impact": "渠道资源变化可能推动品牌预算向内容场、即时场或线下场迁移。",
            "insight_jd_action": "京东商超可对比品牌投放、达人内容、货架转化和大促资源分配的变化。",
        }
    elif category == "开店 / 拓店":
        result = {
            "insight_type": "动作",
            "insight_motive": f"{entity}拓店或调改通常指向区域覆盖、服务体验和规模效率优化。",
            "insight_impact": "门店布局会改变区域竞争半径，也会影响用户对即时购买和线下体验的选择。",
            "insight_jd_action": "京东商超可观察相关城市的用户价格敏感度、到家需求和家庭囤货场景变化。",
        }
    elif category == "科技 / AI":
        result = {
            "insight_type": "关注",
            "insight_motive": f"{entity}投入科技或 AI 能力，背后可能是运营效率、商品推荐和客服体验的改善需求。",
            "insight_impact": "AI 工具可能降低内容、营销和商品运营成本，提升用户触达效率。",
            "insight_jd_action": "京东商超可评估 AI 在选品、补货、营销素材和客服运营中的可落地场景。",
        }
    else:
        result = {
            "insight_type": judgment_type_for_category(category, importance),
            "insight_motive": f"{entity}的外部动作反映其在增长、供给或用户心智上的经营调整。",
            "insight_impact": "连续出现的同类信号可能改变竞对资源投入、品牌合作和品类经营节奏。",
            "insight_jd_action": "京东商超可结合价格、供给、履约和用户反馈，判断是否需要调整对应策略。",
        }

    result["hot_keywords"] = hot_keywords or clean_hot_keywords(CATEGORY_HOT_KEYWORDS.get(category, []), limit=6)
    result["ai_insight"] = f"{result['insight_impact']}{result['insight_jd_action']}"
    return result


def rule_based_brief_body(
    title: str,
    source_excerpt: str,
    summary: str,
) -> str:
    source_text = clean_source_excerpt(source_excerpt or summary, title)
    for phrase in FACT_EXCERPT_BLOCKLIST:
        source_text = source_text.replace(phrase, "")
    sentences = split_sentences(source_text)
    if sentences:
        return join_sentences(sentences, limit=4)
    clean_title = clean_text(title)
    return clean_title[:160] if clean_title else "暂无原文摘要。"


def judgment_type_for_category(category: str, importance: int) -> str:
    if importance >= 5 or category in ("食品安全", "行业政策", "监管合规", "平台政策"):
        return "预警"
    if category in ("新品发布", "行业热点 / 新品趋势", "自有品牌", "爆品", "会员 / 用户"):
        return "机会"
    if category in ("开店 / 拓店", "即时零售", "供应链能力", "渠道动作", "营销活动"):
        return "动作"
    return "关注"


def truncate_display_title(text: str, max_chars: int = 36) -> str:
    text = re.sub(r"\s+", "", clean_text(text))
    text = re.sub(r"[|｜].*$", "", text)
    text = re.sub(r"[-_—–·:：]\s*[^-_—–·:：]{2,30}$", "", text)
    text = text.strip("，。；、：: -_—–")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip("，。；、：: -_—–")


def rule_based_display_title(
    entity: str,
    category: str,
    subcategory: str,
    title: str,
    matched_keywords: List[str],
) -> str:
    entity = clean_text(entity) or "相关主体"
    original_title = truncate_display_title(title, 34)
    if original_title and not is_mostly_english(original_title):
        has_specific_signal = keyword_in_text(entity, original_title) or any(
            keyword_in_text(kw, original_title)
            for kw in matched_keywords
            if kw and len(kw) >= 2 and len(kw) <= 12
        )
        if has_specific_signal and text_signal_length(original_title) >= 10:
            return original_title

    phrase_map = {
        "业务规模 & GMV表现": "经营表现变化值得关注",
        "财报业绩": "营收利润表现释放经营信号",
        "组织架构": "组织调整可能影响业务打法",
        "组织调整": "组织变化影响后续经营节奏",
        "平台政策": "平台规则变化影响商家经营",
        "行业政策": "监管变化影响品类经营",
        "监管合规": "合规要求变化影响经营准入",
        "食品安全": "食品安全事件影响品类准入",
        "开店 / 拓店": "拓店动作影响区域竞争",
        "会员 / 用户": "会员经营动作强化用户粘性",
        "自有品牌": "自有品牌动作强化差异供给",
        "爆品": "爆品表现带动商品策略关注",
        "价格力策略": "价格力动作影响竞争格局",
        "价格变化": "价格波动引发渠道关注",
        "供应链能力": "供应链动作影响履约效率",
        "即时零售": "即时零售布局影响到家竞争",
        "新品发布": "上新动作折射消费趋势",
        "行业热点 / 新品趋势": "新品趋势折射消费变化",
        "重点品牌动态": "品牌动作影响品类竞争",
        "渠道动作": "渠道动作影响终端触达",
        "营销活动": "营销动作影响用户转化",
        "内容电商打法": "内容电商动作影响流量转化",
        "科技 / AI": "AI能力投入影响运营效率",
        "并购合作": "合作动作可能改变竞争格局",
    }
    phrase = phrase_map.get(subcategory) or phrase_map.get(category)
    if phrase:
        return truncate_display_title(f"{entity}{phrase}", 36)

    useful_keywords = [
        kw for kw in matched_keywords
        if kw and kw != entity and len(kw) <= 12 and not is_englishish(kw)
    ]
    if useful_keywords:
        return truncate_display_title(f"{entity}{useful_keywords[0]}动态值得关注", 36)
    return truncate_display_title(title, 36) or "外部动态值得关注"


def rule_based_analysis(
    item: Dict[str, Any],
    monitor: Dict[str, Any],
    taxonomy: Dict[str, Any],
    matched_query: str,
) -> Dict[str, Any]:
    title = item.get("title", "")
    summary = item.get("summary", "")
    source_excerpt = item.get("source_excerpt") or summary
    article_excerpt = item.get("article_excerpt", "")
    text = f"{title} {summary}"
    section = monitor.get("section", "")
    category = infer_category(section, text)
    subcategory = infer_category_subcategory(monitor, text, category)
    entity, entity_type = infer_entity(monitor, text)
    matched_keywords = matched_keywords_for(monitor, title, summary)
    importance = infer_importance(text, taxonomy, category)
    reason_keywords = "、".join(matched_keywords[:8]) if matched_keywords else matched_query
    display_title = rule_based_display_title(entity, category, subcategory, title, matched_keywords)
    structured = rule_based_structured_insight(
        section, entity, category, subcategory, title, summary, matched_keywords, importance
    )

    item.update(
        {
            "section": section,
            "entity": entity,
            "entity_type": entity_type,
            "category_group": monitor.get("display_name", "") if section == "category" else "",
            "category": category,
            "subcategory": "" if subcategory == "全部" else subcategory,
            "matched_query": matched_query,
            "matched_keywords": matched_keywords,
            "importance": importance,
            "display_title": display_title,
            "article_excerpt": article_excerpt if is_valid_excerpt(article_excerpt, title) else "",
            "source_excerpt": clean_source_excerpt(source_excerpt, title),
            "brief_body": rule_based_brief_body(title, article_excerpt or source_excerpt, summary),
            "insight_type": structured.get("insight_type", "关注"),
            "insight_motive": structured.get("insight_motive", ""),
            "insight_impact": structured.get("insight_impact", ""),
            "insight_jd_action": structured.get("insight_jd_action", ""),
            "ai_insight": structured.get("ai_insight", ""),
            "hot_keywords": structured.get("hot_keywords", []),
            "reason": f"关键词与分类信号指向「{category}」；参考词：{reason_keywords}。",
            "analysis_mode": "rule_based",
        }
    )
    return item


def looks_like_noise(title: str, summary: str, taxonomy: Dict[str, Any]) -> bool:
    text = f"{title} {summary}"
    noise_words = taxonomy.get("noise_keywords", []) or []
    if not any(keyword_in_text(word, text) for word in noise_words):
        return False
    high_signal = ("财报", "业绩", "监管", "食品安全", "开店", "战略", "新品", "供应链", "自有品牌")
    return not any(keyword_in_text(word, text) for word in high_signal)


SECTION_SOURCE_PRIORITY = {
    "platform": ["晚点 LatePost", "36氪", "虎嗅消费频道", "虎嗅网", "电商报", "亿邦动力", "亿邦动力专栏", "TechCrunch", "极客公园"],
    "retailer": ["联商网", "北京商报", "北京商报快消", "北京商报电商", "亿邦动力", "亿邦动力专栏", "电商报", "36氪", "职业零售网"],
    "category": ["人民网", "北京商报", "北京商报快消", "中国酒业协会", "中国玩具和婴童用品协会", "中国饮料工业协会", "中国造纸协会生活用纸专业委员会", "中国畜牧业协会", "行业协会网站", "亿邦动力", "联商网"],
}


def preferred_sources_for_section(taxonomy: Dict[str, Any], section: str) -> List[Dict[str, Any]]:
    sources = taxonomy.get("preferred_sources", []) or []
    priority = SECTION_SOURCE_PRIORITY.get(section, [])
    order = {name: index for index, name in enumerate(priority)}

    def source_rank(source: Dict[str, Any]) -> Tuple[int, str]:
        configured_priority = -int(source.get("priority", 0) or 0)
        return (order.get(source.get("name", ""), 999), configured_priority, source.get("name", ""))

    scoped = [
        source for source in sources
        if not source.get("sections") or section in (source.get("sections") or [])
    ]
    ranked = sorted(scoped, key=source_rank)
    return [source for source in ranked if source.get("name") in priority][:6]


def build_preferred_source_queries(
    base_queries: List[str],
    taxonomy: Dict[str, Any],
    section: str,
) -> List[str]:
    sources = preferred_sources_for_section(taxonomy, section)
    source_budget = SECTION_SOURCE_QUERY_BUDGETS.get(section, 0)
    if not sources or source_budget <= 0:
        return []

    source_queries: List[str] = []
    for query in base_queries[:6]:
        for source in sources:
            domain = (source.get("domain") or "").strip()
            source_name = (source.get("name") or "").strip()
            if domain:
                source_queries.append(f"{query} site:{domain}")
            elif source_name:
                source_queries.append(f"{query} {source_name}")
            if len(source_queries) >= source_budget:
                return dedupe_preserve(source_queries)
    return dedupe_preserve(source_queries)


def build_queries(monitor: Dict[str, Any], taxonomy: Dict[str, Any]) -> List[str]:
    queries: List[str] = list(monitor.get("queries", []) or [])
    display = monitor.get("display_name") or monitor.get("name", "")
    aliases = monitor.get("aliases", []) or []
    section = monitor.get("section", "")

    if section in ("platform", "retailer"):
        dimensions = monitor.get("dimensions", []) or []
        queries.extend([display, *aliases[:4]])
        for dimension in dimensions[:8]:
            queries.append(f"{display} {dimension}")
        for alias in aliases[:3]:
            for dimension in dimensions[:3]:
                queries.append(f"{alias} {dimension}")
    else:
        policy_words = monitor.get("policy_keywords", []) or []
        trend_words = monitor.get("trend_keywords", []) or []
        brands = monitor.get("brands", []) or []
        dynamic_words = monitor.get("brand_dynamic_keywords", []) or []
        queries.extend(
            [
                display,
                f"{display} 行业政策",
                f"{display} 新品 趋势",
                f"{display} 监管",
                f"{display} 食品安全",
                f"{display} 重点品牌",
            ]
        )
        for word in policy_words[:5]:
            queries.append(f"{word} 政策 监管")
        for word in trend_words[:5]:
            queries.append(f"{word} 新品 趋势")
        for brand in brands[:10]:
            queries.append(f"{brand} 新品")
            queries.append(f"{brand} 财报")
            queries.append(f"{brand} 渠道")
            queries.append(f"{brand} 价格")
            if dynamic_words:
                queries.append(f"{brand} {dynamic_words[0]}")

    base_queries = dedupe_preserve(queries)
    source_queries = build_preferred_source_queries(base_queries, taxonomy, section)
    if section == "category":
        limit = max(CATEGORY_MIN_QUERIES_PER_MONITOR, CATEGORY_MAX_QUERIES_PER_MONITOR)
        source_reserve = min(8, len(source_queries), SECTION_SOURCE_QUERY_BUDGETS.get(section, 0))
        base_limit = max(CATEGORY_MIN_QUERIES_PER_MONITOR, limit - source_reserve)
        return dedupe_preserve([*base_queries[:base_limit], *source_queries[:source_reserve]])[:limit]
    if MAX_QUERIES_PER_ENTITY > 0:
        source_reserve = min(6, len(source_queries), SECTION_SOURCE_QUERY_BUDGETS.get(section, 0))
        return dedupe_preserve([*base_queries[:MAX_QUERIES_PER_ENTITY], *source_queries[:source_reserve]])
    return []


def google_news_urls(query: str) -> List[str]:
    query_text = f"{query} {QUERY_WINDOW}".strip() if QUERY_WINDOW and "when:" not in query else query
    encoded = urllib.parse.quote_plus(query_text)
    urls = [
        f"https://news.google.com/rss/search?q={encoded}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    ]
    if is_englishish(query):
        urls.append(f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en")
    return urls


def request_url(url: str) -> Optional[bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        warn(f"HTTP {exc.code} while fetching {url}")
    except urllib.error.URLError as exc:
        warn(f"Network error while fetching {url}: {exc.reason}")
    except Exception as exc:
        warn(f"Failed to fetch {url}: {exc}")
    return None


def find_text(element: ET.Element, tag_name: str) -> str:
    child = element.find(tag_name)
    return child.text if child is not None and child.text else ""


def normalize_link(candidate: str) -> str:
    candidate = html.unescape(candidate or "").strip()
    if not candidate or candidate == "#" or candidate.lower().startswith(("javascript:", "mailto:", "about:")):
        return ""
    href_match = re.search(r"https?://[^\s\"'<>]+", candidate)
    if href_match:
        candidate = href_match.group(0)
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    candidate = candidate.strip().rstrip(").,，。")
    if not is_valid_link(candidate):
        return ""
    return candidate


def is_valid_link(link: str) -> bool:
    link = html.unescape(link or "").strip()
    if not link or link == "#":
        return False
    if link.lower().startswith(("javascript:", "mailto:", "about:", "data:")):
        return False
    return bool(re.match(r"^https?://[^\s\"'<>]+$", link, flags=re.I))


def extract_href_from_description(description: str) -> str:
    description = html.unescape(description or "")
    match = re.search(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", description, flags=re.I)
    if match:
        return normalize_link(match.group(1))
    return normalize_link(description)


def extract_best_link(item: ET.Element) -> str:
    """Pick the best URL available in an RSS item.

    Google News RSS usually provides item/link, but some feeds only expose a
    usable URL in guid or in the description's first anchor.
    """
    for candidate in (
        find_text(item, "link"),
        find_text(item, "guid"),
        extract_href_from_description(find_text(item, "description")),
    ):
        link = normalize_link(candidate)
        if link:
            return link
    return ""


def parse_rss_items(content: bytes, fallback_source: str = "") -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        warn(f"RSS parse failed: {exc}")
        return []

    parsed_items: List[Dict[str, str]] = []
    for item in root.findall(".//item")[:MAX_ITEMS_PER_QUERY]:
        source = fallback_source
        source_el = item.find("source")
        if source_el is not None and source_el.text:
            source = clean_text(source_el.text)

        pub_date = find_text(item, "pubDate")
        published_dt = parse_datetime(pub_date)
        published = isoformat_z(published_dt) if published_dt else pub_date
        title = clean_text(find_text(item, "title"))
        raw_description = find_text(item, "description")
        cleaned_description = clean_description(raw_description, title)
        source_excerpt = clean_source_excerpt(raw_description, title)
        link = extract_best_link(item)
        if not is_valid_link(link):
            continue

        parsed_items.append(
            {
                "title": title,
                "link": link,
                "published": published,
                "source": source,
                "summary": source_excerpt or cleaned_description,
                "source_excerpt": source_excerpt,
            }
        )
    return parsed_items


def fetch_rss_for_query(query: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for url in google_news_urls(query):
        content = request_url(url)
        if content:
            items.extend(parse_rss_items(content, fallback_source="Google News RSS"))
        if REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
    return items


def fetch_manual_feed(feed: Any) -> List[Dict[str, str]]:
    if isinstance(feed, str):
        url = feed
        fallback_source = ""
    elif isinstance(feed, dict):
        url = feed.get("url", "")
        fallback_source = feed.get("source", "")
    else:
        return []

    if not url:
        return []
    content = request_url(url)
    if not content:
        return []
    return parse_rss_items(content, fallback_source=fallback_source)


def decode_html_bytes(data: bytes, headers: Any = None) -> str:
    if not data:
        return ""
    encoding = ""
    try:
        if headers and str(headers.get("Content-Encoding", "")).lower() == "gzip":
            data = gzip.decompress(data)
    except Exception:
        pass
    try:
        content_type = headers.get("Content-Type", "") if headers else ""
        match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
        if match:
            encoding = match.group(1)
    except Exception:
        encoding = ""
    for candidate in [encoding, "utf-8", "gb18030", "gbk"]:
        if not candidate:
            continue
        try:
            return data.decode(candidate, errors="ignore")
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def extract_meta_description(page_html: str) -> str:
    patterns = [
        r"<meta\b[^>]*(?:name|property)=[\"']description[\"'][^>]*content=[\"']([^\"']+)[\"'][^>]*>",
        r"<meta\b[^>]*content=[\"']([^\"']+)[\"'][^>]*(?:name|property)=[\"']description[\"'][^>]*>",
        r"<meta\b[^>]*property=[\"']og:description[\"'][^>]*content=[\"']([^\"']+)[\"'][^>]*>",
        r"<meta\b[^>]*name=[\"']twitter:description[\"'][^>]*content=[\"']([^\"']+)[\"'][^>]*>",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            return clean_description(match.group(1))
    return ""


def extract_jsonld_description(page_html: str) -> str:
    blocks = re.findall(
        r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        page_html,
        flags=re.I | re.S,
    )
    for block in blocks[:4]:
        try:
            payload = json.loads(html.unescape(block).strip())
        except Exception:
            continue
        queue = payload if isinstance(payload, list) else [payload]
        while queue:
            current = queue.pop(0)
            if isinstance(current, dict):
                for key in ("description", "articleBody"):
                    value = current.get(key)
                    if isinstance(value, str) and clean_text(value):
                        return clean_description(value)
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        queue.append(value)
            elif isinstance(current, list):
                queue.extend(current)
    return ""


def clean_paragraph_text(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"责任编辑[：:].*$|版权声明.*$|免责声明.*$|本文来源.*$|广告.*$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_paragraph_excerpt(page_html: str, title: str = "") -> str:
    page_html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", page_html)
    article_blocks = re.findall(r"(?is)<article\b[^>]*>(.*?)</article>", page_html)
    candidates_html = article_blocks or [page_html]
    paragraphs: List[str] = []
    for block in candidates_html[:2]:
        for paragraph in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", block):
            text = clean_paragraph_text(paragraph)
            if is_valid_excerpt(text, title):
                paragraphs.append(text)
            if len(paragraphs) >= 4:
                break
        if paragraphs:
            break
    if not paragraphs:
        return ""
    return normalize_excerpt_length("。".join(paragraphs), title)


def normalize_excerpt_length(text: str, title: str = "") -> str:
    text = clean_description(text, title)
    sentences = split_sentences(text)
    if sentences:
        selected: List[str] = []
        length = 0
        for sentence in sentences:
            selected.append(sentence)
            length += text_signal_length(sentence)
            if length >= 80 or len(selected) >= 4:
                break
        text = join_sentences(selected, limit=4)
    if text_signal_length(text) > 240:
        text = text[:260].rstrip("，。；、：: -_—–") + "。"
    return text if is_valid_excerpt(text, title) else ""


def fetch_article_excerpt(url: str, title: str = "") -> str:
    if not is_valid_link(url):
        return ""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=ARTICLE_REQUEST_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower() and "text" not in content_type.lower():
                return ""
            page_html = decode_html_bytes(response.read(ARTICLE_READ_BYTES), response.headers)
    except Exception as exc:
        warn(f"Article excerpt fetch failed for {url}: {exc}")
        return ""
    for extractor in (extract_meta_description, extract_jsonld_description):
        excerpt = normalize_excerpt_length(extractor(page_html), title)
        if excerpt:
            return excerpt
    return extract_paragraph_excerpt(page_html, title)


def enrich_article_excerpts(items: List[Dict[str, Any]], limit: int = ARTICLE_FETCH_LIMIT) -> int:
    if limit <= 0:
        return 0
    count = 0
    for item in items:
        if count >= limit:
            break
        if item.get("article_excerpt") and is_valid_excerpt(item.get("article_excerpt", ""), item.get("title", "")):
            continue
        link = item.get("link", "")
        if not is_valid_link(link):
            continue
        excerpt = fetch_article_excerpt(link, item.get("title", ""))
        count += 1
        if excerpt:
            item["article_excerpt"] = excerpt
    return count


def load_existing_news() -> Dict[str, Any]:
    default = {
        "metadata": {
            "generated_at": "",
            "total": 0,
            "today_count": 0,
            "last_7_days_count": 0,
            "ai_mode": "rule_based",
            "ai_provider": "rule_based",
            "ai_model": "",
            "version": VERSION,
        },
        "summaries": {},
        "briefings": {},
        "items": [],
    }
    data = load_json(NEWS_PATH, default)
    if not isinstance(data, dict):
        return default
    if not isinstance(data.get("items"), list):
        data["items"] = []
    if not isinstance(data.get("metadata"), dict):
        data["metadata"] = default["metadata"]
    if not isinstance(data.get("summaries"), dict):
        data["summaries"] = {}
    if not isinstance(data.get("briefings"), dict):
        data["briefings"] = {}
    return data


def existing_dedupe_sets(items: List[Dict[str, Any]]) -> Tuple[set, set]:
    links = set()
    titles = set()
    for item in items:
        link = (item.get("link") or "").strip().lower()
        norm_title = normalize_title(item.get("title", ""))
        if link:
            links.add(link)
        if norm_title:
            titles.add(norm_title)
    return links, titles


def build_display_excerpt_map(items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    display_map: Dict[str, List[str]] = defaultdict(list)
    for item in items:
        display_key = normalize_title(item.get("display_title", ""))
        if not display_key:
            continue
        excerpt = item.get("source_excerpt") or item.get("summary") or item.get("brief_body") or item.get("title", "")
        display_map[display_key].append(clean_text(excerpt))
    return display_map


def should_skip_duplicate_display_title(
    item: Dict[str, Any],
    display_excerpt_map: Dict[str, List[str]],
) -> bool:
    display_key = normalize_title(item.get("display_title", ""))
    if not display_key or display_key not in display_excerpt_map:
        return False
    excerpt = item.get("source_excerpt") or item.get("summary") or item.get("brief_body") or ""
    if not is_valid_excerpt(excerpt, item.get("title", "")):
        return True
    existing_excerpts = [text for text in display_excerpt_map.get(display_key, []) if text]
    if not existing_excerpts:
        return True
    return any(is_too_similar(excerpt, old, threshold=0.78) for old in existing_excerpts)


def remember_display_title(item: Dict[str, Any], display_excerpt_map: Dict[str, List[str]]) -> None:
    display_key = normalize_title(item.get("display_title", ""))
    if not display_key:
        return
    excerpt = item.get("source_excerpt") or item.get("summary") or item.get("brief_body") or item.get("title", "")
    display_excerpt_map[display_key].append(clean_text(excerpt))


def prune_duplicate_display_titles(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    display_map: Dict[str, List[str]] = defaultdict(list)
    for item in sorted(items, key=sort_key_published, reverse=True):
        if should_skip_duplicate_display_title(item, display_map):
            continue
        kept.append(item)
        remember_display_title(item, display_map)
    return kept


def should_keep_candidate(
    parsed: Dict[str, str],
    monitor: Dict[str, Any],
    taxonomy: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    title = parsed.get("title", "")
    summary = parsed.get("summary", "")
    link = parsed.get("link", "")
    if not title or looks_like_noise(title, summary, taxonomy):
        return False, []
    if not is_valid_link(link):
        return False, []
    if not has_deepseek_api_key() and is_mostly_english(title):
        return False, []
    matches = matched_keywords_for(monitor, title, summary)
    if not matches:
        return False, []
    return True, matches


def fetch_candidates(
    watchlist: Dict[str, Any],
    taxonomy: Dict[str, Any],
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any], str]], Dict[str, Counter]]:
    monitors: List[Dict[str, Any]] = []
    monitors.extend(watchlist.get("platforms", []) or [])
    monitors.extend(watchlist.get("retailers", []) or [])
    monitors.extend(watchlist.get("categories", []) or [])
    candidates: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    stats: Dict[str, Counter] = {section: Counter() for section in ("platform", "retailer", "category")}

    for section in ("platform", "retailer", "category"):
        section_monitors = [
            monitor for monitor in monitors
            if monitor.get("section") == section
        ]
        section_monitors.sort(key=lambda item: int(item.get("priority", 1)), reverse=True)
        query_budget = SECTION_QUERY_BUDGETS.get(section, 0)
        query_count = 0

        for monitor in section_monitors:
            for query in build_queries(monitor, taxonomy):
                if query_budget >= 0 and query_count >= query_budget:
                    break
                query_count += 1
                stats[section]["queries"] += 1
                try:
                    parsed_items = fetch_rss_for_query(query)
                    stats[section]["rss_items"] += len(parsed_items)
                    for parsed in parsed_items:
                        keep, _ = should_keep_candidate(parsed, monitor, taxonomy)
                        if not keep:
                            continue
                        base_item = {
                            "id": stable_id(parsed.get("title", ""), parsed.get("link", "")),
                            "title": parsed.get("title", ""),
                            "link": parsed.get("link", ""),
                            "published": parsed.get("published", ""),
                            "source": parsed.get("source", ""),
                            "summary": parsed.get("summary", ""),
                            "article_excerpt": "",
                            "source_excerpt": parsed.get("source_excerpt", ""),
                            "fetched_at": isoformat_z(utc_now()),
                        }
                        candidates.append((base_item, monitor, query))
                        stats[section]["candidates"] += 1
                except Exception as exc:
                    warn(f"Query failed for {monitor.get('display_name')} / {query}: {exc}")

            for feed in monitor.get("feeds", []) or []:
                try:
                    parsed_items = fetch_manual_feed(feed)
                    stats[section]["manual_feed_items"] += len(parsed_items)
                    for parsed in parsed_items:
                        keep, _ = should_keep_candidate(parsed, monitor, taxonomy)
                        if not keep:
                            continue
                        base_item = {
                            "id": stable_id(parsed.get("title", ""), parsed.get("link", "")),
                            "title": parsed.get("title", ""),
                            "link": parsed.get("link", ""),
                            "published": parsed.get("published", ""),
                            "source": parsed.get("source", ""),
                            "summary": parsed.get("summary", ""),
                            "article_excerpt": "",
                            "source_excerpt": parsed.get("source_excerpt", ""),
                            "fetched_at": isoformat_z(utc_now()),
                        }
                        candidates.append((base_item, monitor, "manual_feed"))
                        stats[section]["candidates"] += 1
                except Exception as exc:
                    warn(f"Manual feed failed for {monitor.get('display_name')}: {exc}")

    return candidates, stats


def build_deepseek_user_prompt(news_batch: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> str:
    schema = {
        "items": [
            {
                "id": "新闻id",
                "is_relevant": True,
                "section": "platform / retailer / category",
                "entity": "命中的平台、零售商、品类或品牌",
                "entity_type": "platform / retailer / category / brand",
                "category": "维度标签",
                "subcategory": "行业政策 / 行业热点 / 新品趋势 / 重点品牌动态 / 空",
                "importance": 1,
                "display_title": "中文浓缩标题，18-32个汉字",
                "brief_body": "2-4句新闻事实简述，不要策略建议",
                "insight_type": "机会 / 预警 / 动作 / 关注",
                "insight_motive": "这件事背后的动机或背景，1句话",
                "insight_impact": "对平台、零售商、品类或竞争格局的影响，1句话",
                "insight_jd_action": "对京东商超的启示或可观察动作，1句话",
                "ai_insight": "整合后的1-2句话中文洞察",
                "hot_keywords": ["业务热词1", "业务热词2", "业务热词3"],
                "reason": "内部判断依据，前端不展示",
            }
        ]
    }
    task = {
        "任务说明": "请从京东商超、京东自营超市和京东平台经营视角，判断新闻是否与平台竞对、零售商、品类政策/热点/品牌动态相关，并完成分类、评分、浓缩标题、正文摘要和商业解读。",
        "板块定义": taxonomy.get("sections", []),
        "分类标签列表": taxonomy.get("global_categories", []),
        "品类子标签": ["行业政策", "行业热点 / 新品趋势", "重点品牌动态", ""],
        "重要性评分规则": {
            "5": "战略调整、组织架构、财报业绩、重大政策监管、食品安全、重大开店/关店、并购、重大合作、商业模式变化。",
            "4": "价格力、自有品牌、会员体系、供应链能力、大促策略、即时零售、平台规则、重点品类趋势。",
            "3": "一般新品、营销活动、渠道动作、品牌传播、常规业务更新。",
            "2": "弱相关、区域性小新闻、单点促销。",
            "1": "低价值或疑似噪音。",
        },
        "输出JSONSchema": schema,
        "要求": [
            "只输出合法 JSON，不要输出 Markdown。",
            "所有输出必须为中文；如果原始新闻是英文，也要用中文生成 display_title、brief_body、ai_insight 和结构化洞察。",
            "display_title 必须是中文，控制在18-32个汉字左右，提炼为“主体 + 动作 + 影响/主题”的一句话。",
            "display_title 必须尽量包含具体主体和具体动作，不要过度泛化，不要把不同新闻都写成“组织调整可能影响业务打法”这类模板句。",
            "display_title 示例：美团称外卖补贴增长不可持续；美团一季度研发投入达70亿元；淘宝闪购首批外卖商户完成打标；抖音达人探索电商结算新路径。",
            "brief_body 必须是中文，2-4句，只基于原始标题、source_excerpt、summary 改写事实：发生了什么、关键数字/时间/主体/动作；不要重复标题，不要输出“建议关注”“对京东而言”“从经营情报看”“该动态涉及”，不要泛泛而谈。",
            "insight_motive 回答为什么这个主体要做这件事，尽量指向增长、利润、履约、低价、会员、供应链、监管、组织效率或用户心智等变量。",
            "insight_impact 回答这件事可能改变什么，尽量指向竞争格局、用户选择、品牌资源、价格心智、履约能力或品类供给。",
            "insight_jd_action 回答京东商超应该观察什么、验证什么信号，或有哪些机会、威胁和动作。",
            "ai_insight 是 insight_impact 与 insight_jd_action 的自然整合，不要简单复述标题。",
            "禁止空泛表达：建议关注其影响、需持续关注、可能产生一定影响、有助于提升竞争力、值得关注、建议关注、该动态涉及。",
            "不要出现“当前重要性为几分”“建议结合命中关键词”“按规则归类为”等系统解释性话术。",
            "hot_keywords 输出3-6个中文业务词，2-8个字优先；不要完整句子，不要“建议关注”“值得关注”“该动态”等套话。",
            "is_relevant=false 的新闻可以标为低价值或噪音。",
        ],
        "待分析新闻数组": news_batch,
    }
    return json.dumps(task, ensure_ascii=False)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", text or "", flags=re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def post_llm_json(
    messages: List[Dict[str, str]],
    max_tokens: int,
    context: str,
) -> Optional[Dict[str, Any]]:
    if not has_llm_api_key():
        return None

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        LLM_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        warn(f"LLM provider {LLM_PROVIDER} {context} request failed; falling back to rule_based: {exc}")
        return None

    try:
        content = response_payload["choices"][0]["message"]["content"]
    except Exception:
        warn(f"LLM provider {LLM_PROVIDER} {context} response missing choices[0].message.content; falling back to rule_based")
        return None
    parsed = extract_json_object(content)
    if parsed is None:
        warn(f"LLM provider {LLM_PROVIDER} {context} JSON parse failed; falling back to rule_based")
    return parsed


def call_llm_chat_completion(
    messages: List[Dict[str, str]],
    max_tokens: int,
    context: str,
) -> Optional[Dict[str, Any]]:
    return post_llm_json(messages, max_tokens, context)


def call_llm_analysis(news_batch: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not has_llm_api_key():
        return None

    system_prompt = (
        "你是“外部动态监控雷达 —— 大商超事业群”的商超与零售竞争情报分析助手。"
        "你要从京东商超、京东自营超市和京东平台经营视角判断新闻对平台竞对、零售商、品类与重点品牌的意义。"
        "brief_body 只写新闻事实简述，不写策略建议。"
        "洞察要回答发生动机、可能影响和京东启示，指向价格、履约、会员、自有品牌、爆品、商家资源、品牌预算、供应链、品类趋势、合规风险等具体经营变量。"
        "display_title、brief_body、ai_insight、insight_motive、insight_impact、insight_jd_action 必须使用中文，不要输出英文标题。"
        "不要出现“当前重要性为几分”“建议结合命中关键词”“按规则归类为”“建议关注其影响”“需持续关注”“该动态涉及”等空泛或系统解释性话术。"
        "你只能输出合法 JSON。不要输出 Markdown。不要输出多余解释。"
    )
    return call_llm_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_deepseek_user_prompt(news_batch, taxonomy)},
        ],
        LLM_MAX_TOKENS,
        "news analysis",
    )


def deepseek_payload_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "article_excerpt": item.get("article_excerpt", ""),
        "source_excerpt": item.get("source_excerpt", "") or item.get("summary", ""),
        "source": item.get("source", ""),
        "published": item.get("published", ""),
        "section_guess": item.get("section", ""),
        "entity_guess": item.get("entity", ""),
        "category_group_guess": item.get("category_group", ""),
        "matched_query": item.get("matched_query", ""),
        "matched_keywords": item.get("matched_keywords", []),
    }


def apply_deepseek_analysis(items: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not has_llm_api_key() or not items:
        return items

    limited = items[:DEEPSEEK_MAX_NEWS_PER_RUN]
    by_id = {item["id"]: item for item in limited}
    irrelevant_ids = set()

    for offset in range(0, len(limited), DEEPSEEK_BATCH_SIZE):
        batch = limited[offset : offset + DEEPSEEK_BATCH_SIZE]
        payload_batch = [deepseek_payload_item(item) for item in batch]
        result = call_llm_analysis(payload_batch, taxonomy)
        if not result:
            continue
        result_items = result.get("items", [])
        if not isinstance(result_items, list):
            warn("LLM returned no items array; keeping rule_based analysis")
            continue

        for analyzed in result_items:
            if not isinstance(analyzed, dict):
                continue
            item_id = analyzed.get("id")
            if item_id not in by_id:
                continue
            if analyzed.get("is_relevant") is False:
                irrelevant_ids.add(item_id)
                continue
            target = by_id[item_id]
            insight_type = analyzed.get("insight_type") or target.get("insight_type") or "关注"
            if insight_type not in INSIGHT_TYPES:
                insight_type = judgment_type_for_category(
                    analyzed.get("category") or target.get("category", "其他"),
                    int(analyzed.get("importance") or target.get("importance") or 1),
                )
            target.update(
                {
                    "section": analyzed.get("section") or target.get("section", ""),
                    "entity": analyzed.get("entity") or target.get("entity", ""),
                    "entity_type": analyzed.get("entity_type") or target.get("entity_type", ""),
                    "category": analyzed.get("category") or target.get("category", "其他"),
                    "subcategory": analyzed.get("subcategory") or "",
                    "importance": int(analyzed.get("importance") or target.get("importance") or 1),
                    "display_title": analyzed.get("display_title") or target.get("display_title", ""),
                    "brief_body": analyzed.get("brief_body") or target.get("brief_body", ""),
                    "insight_type": insight_type,
                    "insight_motive": analyzed.get("insight_motive") or target.get("insight_motive", ""),
                    "insight_impact": analyzed.get("insight_impact") or target.get("insight_impact", ""),
                    "insight_jd_action": analyzed.get("insight_jd_action") or target.get("insight_jd_action", ""),
                    "ai_insight": analyzed.get("ai_insight") or target.get("ai_insight", ""),
                    "hot_keywords": clean_hot_keywords(
                        analyzed.get("hot_keywords") if isinstance(analyzed.get("hot_keywords"), list) else target.get("hot_keywords", []),
                        limit=6,
                    ),
                    "reason": analyzed.get("reason") or target.get("reason", ""),
                    "analysis_mode": ai_provider_name(),
                }
            )
            ensure_source_excerpt(target)
            ensure_brief_body(target)
            ensure_structured_insight(target)

    return [item for item in items if item.get("id") not in irrelevant_ids]


def sort_key_published(item: Dict[str, Any]) -> datetime:
    parsed = parse_datetime(item.get("published", ""))
    return parsed or datetime(1970, 1, 1, tzinfo=timezone.utc)


def build_metadata(items: List[Dict[str, Any]], ai_mode: str) -> Dict[str, Any]:
    now = utc_now()
    local_today = now.astimezone(LOCAL_TZ).date()
    seven_days_ago = now - timedelta(days=7)
    today_count = 0
    last_7_days_count = 0

    for item in items:
        published = parse_datetime(item.get("published", ""))
        if not published:
            continue
        if published.astimezone(LOCAL_TZ).date() == local_today:
            today_count += 1
        if published >= seven_days_ago:
            last_7_days_count += 1

    return {
        "generated_at": isoformat_z(now),
        "total": len(items),
        "today_count": today_count,
        "last_7_days_count": last_7_days_count,
        "ai_mode": ai_mode,
        "ai_provider": ai_mode if ai_mode != "rule_based" else "rule_based",
        "ai_model": LLM_MODEL if ai_mode != "rule_based" and has_llm_api_key() else "",
        "version": VERSION,
    }


def ensure_display_title(item: Dict[str, Any]) -> None:
    if item.get("display_title"):
        return
    title = item.get("title", "")
    entity = item.get("entity", "") or item.get("category_group", "")
    category = item.get("category", "")
    subcategory = item.get("subcategory", "")
    matched_keywords = item.get("matched_keywords", [])
    if not isinstance(matched_keywords, list):
        matched_keywords = []
    item["display_title"] = rule_based_display_title(entity, category, subcategory, title, matched_keywords)


def ensure_source_excerpt(item: Dict[str, Any]) -> None:
    title = item.get("title", "")
    excerpt = item.get("source_excerpt") or item.get("summary", "")
    cleaned = clean_source_excerpt(excerpt, title)
    if cleaned:
        item["source_excerpt"] = cleaned
    else:
        item["source_excerpt"] = ""


def ensure_brief_body(item: Dict[str, Any]) -> None:
    if item.get("brief_body") and not any(phrase in item.get("brief_body", "") for phrase in FACT_EXCERPT_BLOCKLIST):
        return
    item["brief_body"] = rule_based_brief_body(
        item.get("title", ""),
        item.get("article_excerpt", "") or item.get("source_excerpt", ""),
        item.get("summary", ""),
    )


def ensure_structured_insight(item: Dict[str, Any]) -> None:
    missing = (
        not item.get("insight_type")
        or not item.get("insight_motive")
        or not item.get("insight_impact")
        or not item.get("insight_jd_action")
        or not item.get("ai_insight")
        or not item.get("hot_keywords")
    )
    if not missing:
        item["hot_keywords"] = clean_hot_keywords(item.get("hot_keywords", []), limit=6)
        return
    matched_keywords = item.get("matched_keywords", [])
    if not isinstance(matched_keywords, list):
        matched_keywords = []
    structured = rule_based_structured_insight(
        item.get("section", ""),
        item.get("entity", "") or item.get("category_group", ""),
        item.get("category", "其他"),
        item.get("subcategory", ""),
        item.get("title", ""),
        item.get("source_excerpt", "") or item.get("summary", ""),
        matched_keywords,
        int(item.get("importance") or 1),
    )
    for key in ("insight_type", "insight_motive", "insight_impact", "insight_jd_action", "ai_insight", "hot_keywords"):
        if not item.get(key):
            item[key] = structured.get(key, [] if key == "hot_keywords" else "")
    item["hot_keywords"] = clean_hot_keywords(item.get("hot_keywords", []), limit=6)


def judgment_type_for(item: Dict[str, Any]) -> str:
    if item.get("insight_type") in INSIGHT_TYPES:
        return item.get("insight_type", "关注")
    return judgment_type_for_category(item.get("category", ""), int(item.get("importance") or 1))


def default_summary_bullets(section: str) -> List[Dict[str, str]]:
    defaults = {
        "platform": [
            {"type": "预警", "text": "抖音、美团等平台持续加码即时零售，需要关注其对京东超市小时达场景和履约心智的分流。"},
            {"type": "机会", "text": "低价竞争持续强化，京东可在确定性品质、正品心智和品牌合作上形成差异化表达。"},
            {"type": "动作", "text": "建议持续跟踪平台政策、大促资源分配和商家生态变化，判断品牌预算是否向内容场迁移。"},
        ],
        "retailer": [
            {"type": "预警", "text": "会员店和硬折扣持续强化爆品、自有品牌与精选 SKU，可能抬高用户对差异化货盘的期待。"},
            {"type": "机会", "text": "京东可在自营超市中强化高性价比爆品池、家庭囤货场景和稳定履约体验。"},
            {"type": "关注", "text": "线下商超调改与服务体验升级值得持续观察，其变化会影响用户对商超渠道的信任心智。"},
        ],
        "category": [
            {"type": "机会", "text": "健康化、功能化和家庭场景趋势持续出现，适合沉淀趋势货盘和新品首发机会。"},
            {"type": "预警", "text": "食品安全、功效宣称、儿童用品等监管动态需持续关注，避免影响平台品类经营合规。"},
            {"type": "动作", "text": "重点品牌新品和渠道动作可作为京东判断爆品孵化、品牌合作和营销资源倾斜的信号。"},
        ],
    }
    return defaults.get(section, [])


def rule_based_section_summary(section: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    selected = [item for item in items if (item.get("section") or "") == section and item_is_recent(item, 7)]
    if not selected:
        selected = [item for item in items if (item.get("section") or "") == section][:20]
    if not selected:
        bullets = default_summary_bullets(section)
    else:
        top_categories = Counter(item.get("category", "其他") for item in selected if item.get("category"))
        top_entities = Counter((item.get("category_group") or item.get("entity") or "") for item in selected)
        entity = top_entities.most_common(1)[0][0] if top_entities else "外部竞对"
        category = top_categories.most_common(1)[0][0] if top_categories else "经营动态"
        sample = max(selected, key=lambda item: int(item.get("importance") or 1))
        bullets = [
            {"type": judgment_type_for(sample), "text": f"{entity}近期{category}信号更集中，京东商超需关注其对用户心智、货盘竞争和经营节奏的影响。"},
            {"type": "动作", "text": "建议结合高重要性新闻持续跟踪价格、供给、履约和渠道动作，识别可复制的机会与需要防守的风险。"},
            {"type": "关注", "text": "若同一主体或同一维度连续出现，应纳入业务例会的竞对观察和品类策略复盘。"},
        ]
        if section == "category":
            bullets.insert(1, {"type": "预警", "text": "品类政策、食品安全和功效宣称类动态需要优先进入合规与商家准入检查。"})
        elif section == "platform":
            bullets.insert(1, {"type": "预警", "text": "平台侧即时零售、内容电商和商家生态变化，可能影响品牌预算流向和用户购物入口。"})
        elif section == "retailer":
            bullets.insert(1, {"type": "机会", "text": "零售商的自有品牌、爆品与会员动作，可为京东自营超市优化货盘和会员场景提供参照。"})

    return {
        "title": "诸葛参谋",
        "updated_at": isoformat_z(utc_now()),
        "analysis_mode": "rule_based",
        "bullets": bullets[:5],
    }


def build_deepseek_summary_prompt(section: str, items: List[Dict[str, Any]]) -> str:
    slim_items = [
        {
            "id": item.get("id", ""),
            "display_title": item.get("display_title", ""),
            "source_excerpt": item.get("source_excerpt", ""),
            "article_excerpt": item.get("article_excerpt", ""),
            "brief_body": item.get("brief_body", ""),
            "ai_insight": item.get("ai_insight", ""),
            "entity": item.get("entity", ""),
            "category_group": item.get("category_group", ""),
            "category": item.get("category", ""),
            "importance": item.get("importance", 1),
            "source": item.get("source", ""),
            "published": item.get("published", ""),
        }
        for item in items[:30]
    ]
    payload = {
        "任务": "请从京东商超、京东自营超市和京东平台经营视角，基于新闻数组生成本板块的3-5条关键判断。",
        "section": section,
        "输出要求": {
            "title": "诸葛参谋",
            "bullets": "数组，每项包含 type 和 text",
            "type_allowed": ["机会", "预警", "动作", "关注"],
            "text": "必须中文，偏商业判断，不复述标题，不出现系统解释性话术。",
        },
        "新闻数组": slim_items,
    }
    return json.dumps(payload, ensure_ascii=False)


def deepseek_section_summary(section: str, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not has_llm_api_key() or not items:
        return None
    system_prompt = (
        "你是京东大商超事业群的外部经营情报分析助手。"
        "你只输出合法 JSON。所有内容必须中文。"
        "判断必须站在京东商超、京东自营超市、京东平台经营视角，不能复述标题。"
    )
    result = call_llm_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_deepseek_summary_prompt(section, items)},
        ],
        1800,
        f"{section} summary",
    )
    if not result:
        return None
    bullets = result.get("bullets", [])
    if not isinstance(bullets, list):
        return None
    clean_bullets = []
    for bullet in bullets:
        if not isinstance(bullet, dict):
            continue
        bullet_type = bullet.get("type")
        text = clean_text(bullet.get("text", ""))
        if bullet_type not in ("机会", "预警", "动作", "关注") or not text:
            continue
        clean_bullets.append({"type": bullet_type, "text": text})
    if not clean_bullets:
        return None
    return {
        "title": "诸葛参谋",
        "updated_at": isoformat_z(utc_now()),
        "analysis_mode": ai_provider_name(),
        "bullets": clean_bullets[:5],
    }


def build_summaries(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    summaries: Dict[str, Any] = {}
    for section in ("platform", "retailer", "category"):
        recent = [
            item for item in items
            if item.get("section") == section and item_is_recent(item, 7)
        ]
        high_importance = [item for item in recent if int(item.get("importance") or 1) >= 4]
        source_items = high_importance or recent
        source_items = sorted(source_items, key=sort_key_published, reverse=True)[:30]
        summaries[section] = deepseek_section_summary(section, source_items) or rule_based_section_summary(section, items)
    return summaries


GENERIC_DISPLAY_TITLE_PATTERNS = (
    "组织调整可能影响业务打法",
    "即时零售布局影响到家竞争",
    "价格力动作影响竞争格局",
    "平台规则变化影响商家经营",
    "经营表现变化值得关注",
    "外部动态值得关注",
)


def clean_briefing_text(item: Dict[str, Any]) -> str:
    display_title = clean_text(item.get("display_title", ""))
    title = clean_text(item.get("title", ""))
    use_title = (
        not display_title
        or any(pattern in display_title for pattern in GENERIC_DISPLAY_TITLE_PATTERNS)
        or is_too_similar(display_title, f"{item.get('entity', '')}{item.get('category', '')}")
    )
    text = title if use_title and title else display_title or title
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[|｜].*$", "", text).strip("，。；、：: -_—–")
    if len(text) > 40:
        text = text[:40].rstrip("，。；、：: -_—–")
    return text or "外部动态更新"


def briefing_sort_key(item: Dict[str, Any]) -> Tuple[int, datetime, int, int]:
    has_link = 1 if is_valid_link(item.get("link", "")) else 0
    has_body = 1 if any(
        is_valid_excerpt(item.get(key, ""), item.get("title", ""))
        for key in ("article_excerpt", "source_excerpt", "brief_body", "summary")
    ) else 0
    return (
        int(item.get("importance") or 1),
        sort_key_published(item),
        has_link,
        has_body,
    )


def generate_briefings(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    briefings: Dict[str, Any] = {}
    for section in ("platform", "retailer", "category"):
        section_items = [
            item for item in items
            if item.get("section") == section and is_valid_link(item.get("link", ""))
        ]
        recent_items = [item for item in section_items if item_is_recent(item, 7)]
        pool = recent_items if len(recent_items) >= 10 else section_items
        pool = sorted(pool, key=briefing_sort_key, reverse=True)
        seen_titles = set()
        briefing_items: List[Dict[str, Any]] = []
        for item in pool:
            text = clean_briefing_text(item)
            text_key = normalize_title(text)
            if not text_key or text_key in seen_titles:
                continue
            seen_titles.add(text_key)
            entity = item.get("entity") or item.get("category_group") or "外部动态"
            briefing_items.append(
                {
                    "entity": entity,
                    "text": text,
                    "link": item.get("link", ""),
                    "source": item.get("source", ""),
                    "published": item.get("published", ""),
                    "importance": int(item.get("importance") or 1),
                }
            )
            if len(briefing_items) >= 12:
                break
        briefings[section] = {
            "title": "今日快讯",
            "updated_at": isoformat_z(utc_now()),
            "items": briefing_items,
        }
    return briefings


def reanalyze_existing_items(items: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> int:
    if not REANALYZE_EXISTING:
        return 0
    targets = [
        item for item in sorted(items, key=sort_key_published, reverse=True)
        if not item.get("display_title")
        or not item.get("source_excerpt")
        or not item.get("brief_body")
        or not item.get("insight_motive")
        or not item.get("insight_impact")
        or not item.get("insight_jd_action")
        or not item.get("hot_keywords")
        or (has_llm_api_key() and item.get("analysis_mode") != ai_provider_name())
    ]
    if not targets:
        return 0
    if has_llm_api_key():
        apply_deepseek_analysis(targets[:150], taxonomy)
        for item in targets[:150]:
            ensure_source_excerpt(item)
            ensure_display_title(item)
            ensure_brief_body(item)
            ensure_structured_insight(item)
        return min(len(targets), 150)
    for item in targets:
        ensure_source_excerpt(item)
        ensure_display_title(item)
        ensure_brief_body(item)
        ensure_structured_insight(item)
        if not item.get("analysis_mode"):
            item["analysis_mode"] = "rule_based"
    return len(targets)


def print_fetch_stats(stats: Dict[str, Counter], new_counts: Counter) -> None:
    for section in ("platform", "retailer", "category"):
        section_stats = stats.get(section, Counter())
        print(
            f"{section} stats: queries={section_stats.get('queries', 0)}, "
            f"rss_items={section_stats.get('rss_items', 0)}, "
            f"candidates={section_stats.get('candidates', 0)}, "
            f"new_items={new_counts.get(section, 0)}"
        )
    print(f"category new news count: {new_counts.get('category', 0)}")


def main() -> int:
    watchlist = load_json(WATCHLIST_PATH, {"platforms": [], "retailers": [], "categories": []})
    taxonomy = load_json(TAXONOMY_PATH, {})
    existing_data = load_existing_news()
    existing_items = [] if RESET_NEWS else existing_data.get("items", [])
    existing_links, existing_titles = existing_dedupe_sets(existing_items)
    current_links = set(existing_links)
    current_titles = set(existing_titles)
    display_excerpt_map = build_display_excerpt_map(existing_items)
    new_items: List[Dict[str, Any]] = []

    reanalyzed_count = 0 if RESET_NEWS else reanalyze_existing_items(existing_items, taxonomy)
    candidates, fetch_stats = fetch_candidates(watchlist, taxonomy)
    enrich_article_excerpts([base_item for base_item, _, _ in candidates], ARTICLE_FETCH_LIMIT)
    new_counts: Counter = Counter()
    for base_item, monitor, matched_query in candidates:
        link_key = (base_item.get("link") or "").strip().lower()
        title_key = normalize_title(base_item.get("title", ""))
        if link_key and link_key in current_links:
            continue
        if title_key and title_key in current_titles:
            continue
        has_fact_body = any(
            is_valid_excerpt(base_item.get(key, ""), base_item.get("title", ""))
            for key in ("article_excerpt", "source_excerpt", "summary")
        )
        if not has_fact_body and not has_llm_api_key():
            continue

        analyzed = rule_based_analysis(base_item, monitor, taxonomy, matched_query)
        if should_skip_duplicate_display_title(analyzed, display_excerpt_map):
            continue
        new_items.append(analyzed)
        remember_display_title(analyzed, display_excerpt_map)
        new_counts[analyzed.get("section", "")] += 1
        if link_key:
            current_links.add(link_key)
        if title_key:
            current_titles.add(title_key)

    analyzed_new_items = apply_deepseek_analysis(new_items, taxonomy)
    combined_items = existing_items + analyzed_new_items
    for item in combined_items:
        ensure_source_excerpt(item)
        ensure_display_title(item)
        ensure_brief_body(item)
        ensure_structured_insight(item)
    combined_items = prune_duplicate_display_titles(combined_items)
    combined_items.sort(key=sort_key_published, reverse=True)
    combined_items = combined_items[:MAX_HISTORY_ITEMS]

    item_ai_modes = [
        item.get("analysis_mode", "")
        for item in combined_items
        if item.get("analysis_mode") and item.get("analysis_mode") != "rule_based"
    ]
    ai_mode = ai_provider_name() if has_llm_api_key() and item_ai_modes else (item_ai_modes[0] if item_ai_modes else "rule_based")
    summaries = build_summaries(combined_items)
    briefings = generate_briefings(combined_items)
    summary_ai_modes = [
        summary.get("analysis_mode", "")
        for summary in summaries.values()
        if summary.get("analysis_mode") and summary.get("analysis_mode") != "rule_based"
    ]
    if summary_ai_modes:
        ai_mode = ai_provider_name() if has_llm_api_key() else summary_ai_modes[0]
    output = {
        "metadata": build_metadata(combined_items, ai_mode),
        "summaries": summaries,
        "briefings": briefings,
        "items": combined_items,
    }
    write_json_atomic(NEWS_PATH, output)
    print_fetch_stats(fetch_stats, new_counts)
    print(
        f"Generated {NEWS_PATH} with {len(combined_items)} total items "
        f"({len(analyzed_new_items)} new, {reanalyzed_count} reanalyzed, mode={ai_mode})."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        warn(f"Fatal run error; existing news file was not overwritten: {exc}")
        raise SystemExit(1)
