#!/usr/bin/env python3
"""Fetch retail intelligence news from public RSS sources.

The script intentionally uses Python standard library only. It can run with or
without DEEPSEEK_API_KEY. Existing docs/news.json is merged, not replaced.
"""

from __future__ import annotations

import email.utils
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

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_MAX_NEWS_PER_RUN = int(os.getenv("DEEPSEEK_MAX_NEWS_PER_RUN", "200"))
DEEPSEEK_BATCH_SIZE = int(os.getenv("DEEPSEEK_BATCH_SIZE", "20"))
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "5000"))


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
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


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


def rule_based_insight(
    section: str,
    entity: str,
    category: str,
    subcategory: str,
    matched_keywords: List[str],
    importance: int,
) -> str:
    label = subcategory if subcategory and subcategory != "全部" else category
    if category in ("业务规模 & GMV表现", "财报业绩"):
        return f"该动态与{entity}的经营表现相关，建议关注其对增长质量、费用效率和资源投入节奏的影响。"
    if category in ("组织架构", "组织调整"):
        return f"该动态涉及{entity}组织或管理变化，可能影响后续业务优先级、协同效率和竞争打法。"
    if category in ("平台政策", "监管合规", "行业政策", "食品安全"):
        return f"该新闻涉及{label}，需关注其对经营合规、商家准入、品类管理和供应链标准的影响。"
    if category in ("价格力策略", "价格变化"):
        return f"该动态与{entity}价格策略相关，建议关注其对用户转化、毛利结构和竞对跟进动作的影响。"
    if category == "自有品牌":
        return f"该新闻涉及{entity}自有品牌，可能影响差异化商品供给、会员粘性和采购议价空间。"
    if category == "会员 / 用户":
        return f"该动态与{entity}会员或用户经营相关，建议关注其对复购、客单价和长期留存的拉动。"
    if category in ("供应链能力", "即时零售"):
        return f"该动态涉及{entity}供应链或即时履约，建议关注其对履约成本、区域扩张和用户体验的影响。"
    if category in ("新品发布", "行业热点 / 新品趋势", "重点品牌动态"):
        return f"该动态属于{label}，建议关注其背后的健康化、功能化、场景化或渠道化消费需求。"
    if category in ("渠道动作", "营销活动", "内容电商打法"):
        return f"该新闻体现{entity}在渠道或营销侧的动作，建议关注其对流量获取、品牌触达和货架转化的影响。"
    if category == "开店 / 拓店":
        return f"该动态与{entity}门店扩张或调改相关，建议关注区域布局、坪效模型和竞争半径变化。"
    if category == "并购合作":
        return f"该动态涉及{entity}合作或资本动作，可能改变资源配置、渠道覆盖或行业竞争格局。"
    if category == "科技 / AI":
        return f"该动态与{entity}科技或 AI 能力相关，建议关注其对商品运营、营销效率和用户服务的潜在改造。"
    return f"该动态与{entity}相关，建议关注其是否带来外部竞争、用户心智或品类经营节奏的变化。"


def rule_based_brief_body(
    title: str,
    summary: str,
    ai_insight: str,
    entity: str,
    category: str,
    section: str,
) -> str:
    summary_sentences = split_sentences(summary)
    sentences: List[str] = []
    if summary_sentences:
        sentences.extend(summary_sentences[:2])
    elif title:
        sentences.append(f"这条动态提到{clean_text(title)}")

    if category:
        sentences.append(f"从经营情报看，它更偏向{category}信号。")

    if ai_insight:
        sentences.extend(split_sentences(ai_insight)[:2])

    if section == "platform":
        sentences.append("对京东商超而言，可重点观察其对平台流量、商家资源和履约心智的影响。")
    elif section == "retailer":
        sentences.append("对京东商超而言，可重点观察其对差异化货盘、会员经营和价格心智的影响。")
    elif section == "category":
        sentences.append("对京东商超而言，可重点观察其对趋势货盘、品牌合作和品类合规的影响。")
    else:
        sentences.append(f"对京东商超而言，可持续跟踪{entity or '相关主体'}后续动作。")

    return join_sentences(sentences, limit=5)


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
    text = f"{title} {summary}"
    section = monitor.get("section", "")
    category = infer_category(section, text)
    subcategory = infer_category_subcategory(monitor, text, category)
    entity, entity_type = infer_entity(monitor, text)
    matched_keywords = matched_keywords_for(monitor, title, summary)
    importance = infer_importance(text, taxonomy, category)
    reason_keywords = "、".join(matched_keywords[:8]) if matched_keywords else matched_query
    display_title = rule_based_display_title(entity, category, subcategory, title, matched_keywords)
    ai_insight = rule_based_insight(section, entity, category, subcategory, matched_keywords, importance)

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
            "brief_body": rule_based_brief_body(title, summary, ai_insight, entity, category, section),
            "ai_insight": ai_insight,
            "reason": f"命中关键词：{reason_keywords}；主要信号指向「{category}」。",
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
    "platform": ["36氪", "虎嗅网", "亿邦动力", "TechCrunch", "极客公园"],
    "retailer": ["联商网", "亿邦动力", "北京商报", "36氪", "职业零售网"],
    "category": ["人民网", "北京商报", "行业协会网站", "酒类商业协会", "亿邦动力", "联商网"],
}


def preferred_sources_for_section(taxonomy: Dict[str, Any], section: str) -> List[Dict[str, Any]]:
    sources = taxonomy.get("preferred_sources", []) or []
    priority = SECTION_SOURCE_PRIORITY.get(section, [])
    order = {name: index for index, name in enumerate(priority)}

    def source_rank(source: Dict[str, Any]) -> Tuple[int, str]:
        return (order.get(source.get("name", ""), 999), source.get("name", ""))

    ranked = sorted(sources, key=source_rank)
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
    if not candidate:
        return ""
    href_match = re.search(r"https?://[^\s\"'<>]+", candidate)
    if href_match:
        candidate = href_match.group(0)
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    if not re.match(r"^https?://", candidate, flags=re.I):
        return ""
    return candidate.strip().rstrip(").,，。")


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

        parsed_items.append(
            {
                "title": clean_text(find_text(item, "title")),
                "link": extract_best_link(item),
                "published": published,
                "source": source,
                "summary": clean_text(find_text(item, "description")),
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


def load_existing_news() -> Dict[str, Any]:
    default = {
        "metadata": {
            "generated_at": "",
            "total": 0,
            "today_count": 0,
            "last_7_days_count": 0,
            "ai_mode": "rule_based",
            "version": VERSION,
        },
        "summaries": {},
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


def should_keep_candidate(
    parsed: Dict[str, str],
    monitor: Dict[str, Any],
    taxonomy: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    title = parsed.get("title", "")
    summary = parsed.get("summary", "")
    if not title or looks_like_noise(title, summary, taxonomy):
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
                "brief_body": "3-5句中文正文摘要",
                "ai_insight": "1-2句话中文商业解读",
                "reason": "简短判断依据",
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
            "不要简单复述标题，ai_insight 要体现商业判断。",
            "display_title 必须是中文，控制在18-32个汉字左右，提炼为“主体 + 动作 + 影响/主题”的一句话。",
            "brief_body 必须是中文，3-5句，说明新闻说了什么、对平台/零售商/品类意味着什么、对京东商超的潜在影响或观察点。",
            "ai_insight 必须是中文，不能输出英文标题。",
            "ai_insight 不要出现“当前重要性为几分”“建议结合命中关键词”“按规则归类为”等系统解释性话术。",
            "如果原始新闻是英文，也要用中文生成 display_title、brief_body 和 ai_insight。",
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


def post_deepseek_json(
    messages: List[Dict[str, str]],
    max_tokens: int,
    context: str,
) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        DEEPSEEK_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        warn(f"DeepSeek {context} request failed; falling back to rule_based: {exc}")
        return None

    try:
        content = response_payload["choices"][0]["message"]["content"]
    except Exception:
        warn(f"DeepSeek {context} response missing choices[0].message.content; falling back to rule_based")
        return None
    parsed = extract_json_object(content)
    if parsed is None:
        warn(f"DeepSeek {context} JSON parse failed; falling back to rule_based")
    return parsed


def call_deepseek(news_batch: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    system_prompt = (
        "你是“外部动态监控雷达 —— 大商超事业群”的商超与零售竞争情报分析助手。"
        "你要从京东商超、京东自营超市和京东平台经营视角判断新闻对平台竞对、零售商、品类与重点品牌的意义。"
        "你的解读要偏商业判断，不要简单复述标题。"
        "display_title、brief_body 和 ai_insight 必须使用中文，不要输出英文标题。"
        "不要出现“当前重要性为几分”“建议结合命中关键词”“按规则归类为”等系统解释性话术。"
        "你只能输出合法 JSON。不要输出 Markdown。不要输出多余解释。"
    )
    return post_deepseek_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_deepseek_user_prompt(news_batch, taxonomy)},
        ],
        DEEPSEEK_MAX_TOKENS,
        "news analysis",
    )


def deepseek_payload_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "source": item.get("source", ""),
        "published": item.get("published", ""),
        "section_guess": item.get("section", ""),
        "entity_guess": item.get("entity", ""),
        "category_group_guess": item.get("category_group", ""),
        "matched_query": item.get("matched_query", ""),
        "matched_keywords": item.get("matched_keywords", []),
    }


def apply_deepseek_analysis(items: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> List[Dict[str, Any]]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key or not items:
        return items

    limited = items[:DEEPSEEK_MAX_NEWS_PER_RUN]
    by_id = {item["id"]: item for item in limited}
    irrelevant_ids = set()

    for offset in range(0, len(limited), DEEPSEEK_BATCH_SIZE):
        batch = limited[offset : offset + DEEPSEEK_BATCH_SIZE]
        payload_batch = [deepseek_payload_item(item) for item in batch]
        result = call_deepseek(payload_batch, taxonomy)
        if not result:
            continue
        result_items = result.get("items", [])
        if not isinstance(result_items, list):
            warn("DeepSeek returned no items array; keeping rule_based analysis")
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
                    "ai_insight": analyzed.get("ai_insight") or target.get("ai_insight", ""),
                    "reason": analyzed.get("reason") or target.get("reason", ""),
                    "analysis_mode": "deepseek",
                }
            )

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


def ensure_brief_body(item: Dict[str, Any]) -> None:
    if item.get("brief_body"):
        return
    item["brief_body"] = rule_based_brief_body(
        item.get("title", ""),
        item.get("summary", ""),
        item.get("ai_insight", ""),
        item.get("entity", "") or item.get("category_group", ""),
        item.get("category", ""),
        item.get("section", ""),
    )


def judgment_type_for(item: Dict[str, Any]) -> str:
    category = item.get("category", "")
    importance = int(item.get("importance") or 1)
    if category in ("食品安全", "行业政策", "监管合规", "平台政策") or importance >= 5:
        return "预警"
    if category in ("新品发布", "行业热点 / 新品趋势", "自有品牌", "爆品", "会员 / 用户"):
        return "机会"
    if category in ("开店 / 拓店", "即时零售", "供应链能力", "渠道动作", "营销活动"):
        return "动作"
    return "关注"


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
        "title": "京东视角 · 今日关键判断",
        "updated_at": isoformat_z(utc_now()),
        "analysis_mode": "rule_based",
        "bullets": bullets[:5],
    }


def build_deepseek_summary_prompt(section: str, items: List[Dict[str, Any]]) -> str:
    slim_items = [
        {
            "id": item.get("id", ""),
            "display_title": item.get("display_title", ""),
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
            "title": "京东视角 · 今日关键判断",
            "bullets": "数组，每项包含 type 和 text",
            "type_allowed": ["机会", "预警", "动作", "关注"],
            "text": "必须中文，偏商业判断，不复述标题，不出现系统解释性话术。",
        },
        "新闻数组": slim_items,
    }
    return json.dumps(payload, ensure_ascii=False)


def deepseek_section_summary(section: str, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not has_deepseek_api_key() or not items:
        return None
    system_prompt = (
        "你是京东大商超事业群的外部经营情报分析助手。"
        "你只输出合法 JSON。所有内容必须中文。"
        "判断必须站在京东商超、京东自营超市、京东平台经营视角，不能复述标题。"
    )
    result = post_deepseek_json(
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
        "title": "京东视角 · 今日关键判断",
        "updated_at": isoformat_z(utc_now()),
        "analysis_mode": "deepseek",
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


def reanalyze_existing_items(items: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> int:
    if not REANALYZE_EXISTING:
        return 0
    targets = [
        item for item in sorted(items, key=sort_key_published, reverse=True)
        if not item.get("display_title")
        or not item.get("brief_body")
        or (has_deepseek_api_key() and item.get("analysis_mode") != "deepseek")
    ]
    if not targets:
        return 0
    if has_deepseek_api_key():
        apply_deepseek_analysis(targets[:150], taxonomy)
        for item in targets[:150]:
            ensure_display_title(item)
            ensure_brief_body(item)
        return min(len(targets), 150)
    for item in targets:
        ensure_display_title(item)
        ensure_brief_body(item)
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
    existing_items = existing_data.get("items", [])
    existing_links, existing_titles = existing_dedupe_sets(existing_items)
    current_links = set(existing_links)
    current_titles = set(existing_titles)
    new_items: List[Dict[str, Any]] = []

    reanalyzed_count = reanalyze_existing_items(existing_items, taxonomy)
    candidates, fetch_stats = fetch_candidates(watchlist, taxonomy)
    new_counts: Counter = Counter()
    for base_item, monitor, matched_query in candidates:
        link_key = (base_item.get("link") or "").strip().lower()
        title_key = normalize_title(base_item.get("title", ""))
        if link_key and link_key in current_links:
            continue
        if title_key and title_key in current_titles:
            continue

        analyzed = rule_based_analysis(base_item, monitor, taxonomy, matched_query)
        new_items.append(analyzed)
        new_counts[analyzed.get("section", "")] += 1
        if link_key:
            current_links.add(link_key)
        if title_key:
            current_titles.add(title_key)

    analyzed_new_items = apply_deepseek_analysis(new_items, taxonomy)
    combined_items = existing_items + analyzed_new_items
    for item in combined_items:
        ensure_display_title(item)
        ensure_brief_body(item)
    combined_items.sort(key=sort_key_published, reverse=True)
    combined_items = combined_items[:MAX_HISTORY_ITEMS]

    ai_mode = "deepseek" if any(item.get("analysis_mode") == "deepseek" for item in combined_items) else "rule_based"
    summaries = build_summaries(combined_items)
    if any(summary.get("analysis_mode") == "deepseek" for summary in summaries.values()):
        ai_mode = "deepseek"
    output = {
        "metadata": build_metadata(combined_items, ai_mode),
        "summaries": summaries,
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
