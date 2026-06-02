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
MAX_TOTAL_QUERIES = int(os.getenv("RETAIL_NEWS_MAX_TOTAL_QUERIES", "220"))
MAX_HISTORY_ITEMS = int(os.getenv("RETAIL_NEWS_MAX_HISTORY_ITEMS", "1500"))
REQUEST_TIMEOUT = int(os.getenv("RETAIL_NEWS_REQUEST_TIMEOUT", "12"))
REQUEST_DELAY_SECONDS = float(os.getenv("RETAIL_NEWS_REQUEST_DELAY_SECONDS", "0.08"))
QUERY_WINDOW = os.getenv("RETAIL_NEWS_QUERY_WINDOW", "when:30d").strip()

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


def is_englishish(value: str) -> bool:
    letters = re.findall(r"[A-Za-z]", value or "")
    return len(letters) >= 3


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
    return f"该动态与{entity}相关，当前重要性为{importance}分，建议结合命中关键词判断是否纳入后续重点跟踪。"


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
            "ai_insight": rule_based_insight(section, entity, category, subcategory, matched_keywords, importance),
            "reason": f"命中关键词：{reason_keywords}；按规则归类为「{category}」。",
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


def build_queries(monitor: Dict[str, Any]) -> List[str]:
    queries: List[str] = list(monitor.get("queries", []) or [])
    display = monitor.get("display_name") or monitor.get("name", "")
    aliases = monitor.get("aliases", []) or []

    if monitor.get("section") in ("platform", "retailer"):
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
        queries.append(display)
        for word in policy_words[:6]:
            queries.append(f"{word} 政策 监管")
        for word in trend_words[:6]:
            queries.append(f"{word} 新品 趋势")
        for brand in brands[:12]:
            queries.append(f"{brand} {dynamic_words[0] if dynamic_words else '动态'}")
            queries.append(f"{brand} 新品 渠道")

    queries = dedupe_preserve(queries)
    if MAX_QUERIES_PER_ENTITY > 0:
        return queries[:MAX_QUERIES_PER_ENTITY]
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
                "link": clean_text(find_text(item, "link")),
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
        "items": [],
    }
    data = load_json(NEWS_PATH, default)
    if not isinstance(data, dict):
        return default
    if not isinstance(data.get("items"), list):
        data["items"] = []
    if not isinstance(data.get("metadata"), dict):
        data["metadata"] = default["metadata"]
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
    matches = matched_keywords_for(monitor, title, summary)
    if not matches:
        return False, []
    return True, matches


def fetch_candidates(watchlist: Dict[str, Any], taxonomy: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any], str]]:
    monitors: List[Dict[str, Any]] = []
    monitors.extend(watchlist.get("platforms", []) or [])
    monitors.extend(watchlist.get("retailers", []) or [])
    monitors.extend(watchlist.get("categories", []) or [])
    monitors.sort(key=lambda item: int(item.get("priority", 1)), reverse=True)

    candidates: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    query_counter = 0

    for monitor in monitors:
        for query in build_queries(monitor):
            if MAX_TOTAL_QUERIES >= 0 and query_counter >= MAX_TOTAL_QUERIES:
                return candidates
            query_counter += 1
            try:
                for parsed in fetch_rss_for_query(query):
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
            except Exception as exc:
                warn(f"Query failed for {monitor.get('display_name')} / {query}: {exc}")

        for feed in monitor.get("feeds", []) or []:
            try:
                for parsed in fetch_manual_feed(feed):
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
            except Exception as exc:
                warn(f"Manual feed failed for {monitor.get('display_name')}: {exc}")

    return candidates


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
                "subcategory": "行业政策 / 行业热点 / 重点品牌动态 / 空",
                "importance": 1,
                "ai_insight": "1-2句话商业解读",
                "reason": "简短判断依据",
            }
        ]
    }
    task = {
        "任务说明": "请从商分和商超业务视角判断新闻是否与平台竞对、零售商、品类政策/热点/品牌动态相关，并完成分类、评分和商业解读。",
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


def call_deepseek(news_batch: List[Dict[str, Any]], taxonomy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    system_prompt = (
        "你是“外部动态监控雷达 —— 大商超事业群”的商超与零售竞争情报分析助手。"
        "你要从商分视角判断新闻对平台竞对、零售商、品类与重点品牌的意义。"
        "你的解读要偏商业判断，不要简单复述标题。"
        "你只能输出合法 JSON。不要输出 Markdown。不要输出多余解释。"
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_deepseek_user_prompt(news_batch, taxonomy)},
        ],
        "temperature": 0.2,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
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
        warn(f"DeepSeek request failed; falling back to rule_based: {exc}")
        return None

    try:
        content = response_payload["choices"][0]["message"]["content"]
    except Exception:
        warn("DeepSeek response missing choices[0].message.content; falling back to rule_based")
        return None
    parsed = extract_json_object(content)
    if parsed is None:
        warn("DeepSeek JSON parse failed; falling back to rule_based")
    return parsed


def deepseek_payload_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "source": item.get("source", ""),
        "published": item.get("published", ""),
        "section_guess": item.get("section", ""),
        "entity_guess": item.get("entity", ""),
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


def main() -> int:
    watchlist = load_json(WATCHLIST_PATH, {"platforms": [], "retailers": [], "categories": []})
    taxonomy = load_json(TAXONOMY_PATH, {})
    existing_data = load_existing_news()
    existing_items = existing_data.get("items", [])
    existing_links, existing_titles = existing_dedupe_sets(existing_items)
    current_links = set(existing_links)
    current_titles = set(existing_titles)
    new_items: List[Dict[str, Any]] = []

    candidates = fetch_candidates(watchlist, taxonomy)
    for base_item, monitor, matched_query in candidates:
        link_key = (base_item.get("link") or "").strip().lower()
        title_key = normalize_title(base_item.get("title", ""))
        if link_key and link_key in current_links:
            continue
        if title_key and title_key in current_titles:
            continue

        analyzed = rule_based_analysis(base_item, monitor, taxonomy, matched_query)
        new_items.append(analyzed)
        if link_key:
            current_links.add(link_key)
        if title_key:
            current_titles.add(title_key)

    analyzed_new_items = apply_deepseek_analysis(new_items, taxonomy)
    combined_items = existing_items + analyzed_new_items
    combined_items.sort(key=sort_key_published, reverse=True)
    combined_items = combined_items[:MAX_HISTORY_ITEMS]

    ai_mode = "deepseek" if any(item.get("analysis_mode") == "deepseek" for item in analyzed_new_items) else "rule_based"
    output = {
        "metadata": build_metadata(combined_items, ai_mode),
        "items": combined_items,
    }
    write_json_atomic(NEWS_PATH, output)
    print(
        f"Generated {NEWS_PATH} with {len(combined_items)} total items "
        f"({len(analyzed_new_items)} new, mode={ai_mode})."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        warn(f"Fatal run error; existing news file was not overwritten: {exc}")
        raise SystemExit(1)
