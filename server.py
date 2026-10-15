from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


SERVER_NAME = "cn-ecommerce"
SO_SEARCH_URL = "https://www.so.com/s"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
MIN_REQUEST_INTERVAL_SECONDS = 0.8
CACHE_TTL_SECONDS = 600

PLATFORMS: dict[str, dict[str, Any]] = {
    "pdd": {
        "display_name": "拼多多",
        "direct_query": None,
        "deal_query": "{keyword} 拼多多 优惠 价格",
        "host_suffixes": ("yangkeduo.com", "pinduoduo.com"),
    },
    "jd": {
        "display_name": "京东",
        "direct_query": "site:item.jd.com {keyword}",
        "deal_query": "{keyword} 京东 多少钱 什么值得买",
        "host_suffixes": ("jd.com",),
    },
    "taobao": {
        "display_name": "淘宝",
        "direct_query": "site:item.taobao.com {keyword}",
        "deal_query": "{keyword} 淘宝 优惠 价格",
        "host_suffixes": ("taobao.com",),
    },
}

PUBLIC_DEAL_SOURCE_SUFFIXES = ("smzdm.com", "zol.com.cn")

RESULT_BLOCK_PATTERN = re.compile(
    r'<li class="res-list"[^>]*>(?P<body>.*?)</li>', re.DOTALL | re.IGNORECASE
)
TITLE_PATTERN = re.compile(
    r'<h3\s+class="[^"]*\bres-title\b[^"]*"[^>]*>.*?<a(?P<attrs>[^>]*)>'
    r'(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
DIRECT_URL_PATTERN = re.compile(r'data-mdurl="(?P<url>[^"]+)"', re.IGNORECASE)
HREF_PATTERN = re.compile(r'href="(?P<url>[^"]+)"', re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")
DATE_PATTERN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
CURRENCY_PRICE_PATTERN = re.compile(
    r"[¥￥]\s*(?P<price>\d{1,7}(?:\.\d{1,2})?)"
)
NAMED_PRICE_PATTERN = re.compile(
    r"(?:到手实付|实付低至|最终到手价|到手价|活动售价|页面价|售价|原价)"
    r"[^\d]{0,8}(?P<price>\d{1,7}(?:\.\d{1,2})?)\s*元?"
)
FINAL_PRICE_PATTERNS = (
    re.compile(
        r"(?:到手实付|实付低至|最终到手价|到手价)"
        r"[^\d]{0,8}(\d{1,7}(?:\.\d{1,2})?)\s*元?"
    ),
    CURRENCY_PRICE_PATTERN,
    re.compile(
        r"(?:活动售价|页面价|售价)[^\d]{0,8}"
        r"(\d{1,7}(?:\.\d{1,2})?)\s*元?"
    ),
)
ORIGINAL_PRICE_PATTERN = re.compile(
    r"原价[^\d]{0,8}(\d{1,7}(?:\.\d{1,2})?)\s*元?"
)
PROMOTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "满减/优惠券",
        re.compile(
            r"满\s*\d+(?:\.\d+)?\s*元?\s*减\s*\d+(?:\.\d+)?\s*元?"
            r"|优惠券|优惠卷"
        ),
    ),
    ("折扣", re.compile(r"\d(?:\.\d+)?\s*折")),
    ("立减", re.compile(r"立减\s*\d+(?:\.\d+)?\s*元?")),
    ("直降", re.compile(r"直降\s*\d+(?:\.\d+)?\s*元?")),
    ("百亿补贴", re.compile(r"百亿补贴")),
    ("限时活动", re.compile(r"限时|活动售价|近期好价")),
    ("包邮/免运费", re.compile(r"包邮|免运费")),
    ("登录后优惠详情", re.compile(r"登录查看更多优惠|登录后查看更多")),
)

_cache: dict[tuple[str, str, int, float | None], tuple[float, dict[str, Any]]] = {}
_request_lock = asyncio.Lock()
_last_request_at = 0.0


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_keyword(keyword: str) -> str:
    normalized = " ".join(keyword.split())
    if not normalized:
        raise ValueError("keyword must not be empty")
    if len(normalized) > 120:
        raise ValueError("keyword must be at most 120 characters")
    return normalized


def _validate_platform(platform: str, *, allow_all: bool = False) -> str:
    normalized = platform.strip().lower()
    allowed = set(PLATFORMS)
    if allow_all:
        allowed.add("all")
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"unsupported platform: {platform!r}; choose one of {choices}")
    return normalized


def _build_one_search_link(keyword: str, platform: str) -> str:
    if platform == "pdd":
        return "https://mobile.yangkeduo.com/search_result.html?" + urlencode(
            {"search_key": keyword}
        )
    if platform == "jd":
        return "https://search.jd.com/Search?" + urlencode(
            {"keyword": keyword, "enc": "utf-8"}
        )
    if platform == "taobao":
        return "https://s.taobao.com/search?" + urlencode({"q": keyword})
    raise ValueError(f"unsupported platform: {platform}")


def _clean_html(value: str) -> str:
    return " ".join(html.unescape(TAG_PATTERN.sub(" ", value)).split())


def _hostname_matches(hostname: str, suffixes: tuple[str, ...]) -> bool:
    hostname = hostname.lower()
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes
    )


def _official_product_url(url: str, platform: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not _hostname_matches(hostname, PLATFORMS[platform]["host_suffixes"]):
        return False
    if platform == "jd":
        return bool(re.search(r"/\d+\.html(?:$|[?#])", url))
    if platform == "taobao":
        return hostname == "item.taobao.com" or hostname.endswith(".item.taobao.com")
    if platform == "pdd":
        return bool((parse_qs(parsed.query).get("goods_id") or [None])[0])
    return False


def _allowed_source_url(url: str, platform: str, query_kind: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    if query_kind == "direct":
        return _hostname_matches(hostname, PLATFORMS[platform]["host_suffixes"])
    return _hostname_matches(
        hostname,
        (*PLATFORMS[platform]["host_suffixes"], *PUBLIC_DEAL_SOURCE_SUFFIXES),
    )


def _extract_prices(text: str) -> list[float]:
    values: list[float] = []
    for pattern in (CURRENCY_PRICE_PATTERN, NAMED_PRICE_PATTERN):
        for match in pattern.finditer(text):
            value = float(match.group("price"))
            if 0 < value < 1_000_000 and value not in values:
                values.append(value)
    return values


def _first_pattern_price(text: str) -> float | None:
    for pattern in FINAL_PRICE_PATTERNS:
        match = pattern.search(text)
        if match:
            group = match.groupdict().get("price") or match.group(1)
            return float(group)
    return None


def _promotion_labels(text: str) -> list[str]:
    return [label for label, pattern in PROMOTION_PATTERNS if pattern.search(text)]


def _indexed_date(text: str) -> str | None:
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        ).date().isoformat()
    except ValueError:
        return None


def _freshness(indexed_date: str | None) -> str:
    if not indexed_date:
        return "unknown"
    observed = datetime.fromisoformat(indexed_date).date()
    age_days = (datetime.now(timezone.utc).date() - observed).days
    if age_days < -2:
        return "future_dated_index_entry"
    if age_days <= 120:
        return "recent_index_entry"
    return "stale_index_entry"


def _price_kind(text: str, price: float | None) -> str:
    if price is None:
        return "unavailable"
    if re.search(r"到手实付|实付低至|最终到手价|到手价", text):
        return "claimed_final_price"
    if re.search(r"活动售价|页面价|售价", text):
        return "claimed_activity_price"
    return "indexed_display_price"


def _extract_result_url(block: str, attrs: str) -> str | None:
    match = DIRECT_URL_PATTERN.search(block) or HREF_PATTERN.search(attrs)
    if not match:
        return None
    url = html.unescape(match.group("url")).strip()
    return "https://" + url[7:] if url.startswith("http://") else url


def _parse_so_results(
    page: str,
    platform: str,
    query_kind: str,
    limit: int,
    max_price_cny: float | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    platform_name = PLATFORMS[platform]["display_name"]

    for block_match in RESULT_BLOCK_PATTERN.finditer(page):
        block = block_match.group("body")
        title_match = TITLE_PATTERN.search(block)
        if not title_match:
            continue
        title = _clean_html(title_match.group("title"))
        source_url = _extract_result_url(block, title_match.group("attrs"))
        if not title or not source_url or source_url in seen_urls:
            continue
        if not _allowed_source_url(source_url, platform, query_kind):
            continue

        snippet = _clean_html(block)
        if query_kind == "deal" and platform_name not in snippet:
            continue

        prices = _extract_prices(snippet)
        displayed_price = _first_pattern_price(snippet)
        original_match = ORIGINAL_PRICE_PATTERN.search(snippet)
        original_price = float(original_match.group(1)) if original_match else None
        product_url = source_url if _official_product_url(source_url, platform) else None
        indexed = _indexed_date(snippet)
        promotions = _promotion_labels(snippet)
        budget_match = (
            None
            if max_price_cny is None or displayed_price is None
            else displayed_price <= max_price_cny
        )
        if max_price_cny is not None and budget_match is False:
            continue

        parsed_link = parse_product_link(product_url) if product_url else None
        results.append(
            {
                "platform": platform,
                "platform_name": platform_name,
                "title": title,
                "displayed_price_cny": displayed_price,
                "price_kind": _price_kind(snippet, displayed_price),
                "original_price_cny": original_price,
                "all_prices_cny": prices,
                "general_promotions": promotions,
                "product_url": product_url,
                "source_url": source_url,
                "best_available_link": product_url or source_url,
                "link_type": "direct_product" if product_url else "promotion_source",
                "product_id": parsed_link.get("product_id") if parsed_link else None,
                "source_host": (urlparse(source_url).hostname or "").lower(),
                "source_type": (
                    "official_product_index"
                    if product_url
                    else "public_promotion_or_catalog_index"
                ),
                "source": "360_public_web_index",
                "indexed_date": indexed,
                "freshness": _freshness(indexed),
                "price_confidence": (
                    "indexed_price_only" if displayed_price is not None else "unavailable"
                ),
                "matches_budget": budget_match,
                "snippet": snippet[:360],
            }
        )
        seen_urls.add(source_url)

    results.sort(
        key=lambda item: (
            item["displayed_price_cny"] is not None,
            bool(item["general_promotions"]),
            item["product_url"] is not None,
            item["freshness"] == "recent_index_entry",
        ),
        reverse=True,
    )
    return results[:limit]


async def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        await asyncio.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)


async def _fetch_so_page(query: str) -> str:
    global _last_request_at
    async with _request_lock:
        await _throttle()
        async with httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            },
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            response = await client.get(SO_SEARCH_URL, params={"q": query})
            _last_request_at = time.monotonic()
            response.raise_for_status()
            return response.text


async def _search_one_platform(
    keyword: str,
    platform: str,
    limit: int,
    max_price_cny: float | None,
) -> dict[str, Any]:
    cache_key = (platform, keyword, limit, max_price_cny)
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        payload = dict(cached[1])
        payload["cache_hit"] = True
        return payload

    config = PLATFORMS[platform]
    query_specs: list[tuple[str, str]] = []
    if config["direct_query"]:
        query_specs.append(("direct", config["direct_query"].format(keyword=keyword)))
    query_specs.append(("deal", config["deal_query"].format(keyword=keyword)))

    listings: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_links: set[str] = set()
    for query_kind, query in query_specs:
        try:
            page = await _fetch_so_page(query)
            parsed = _parse_so_results(
                page,
                platform=platform,
                query_kind=query_kind,
                limit=limit,
                max_price_cny=max_price_cny,
            )
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{query_kind}: {type(exc).__name__}: {exc}")
            continue
        for item in parsed:
            link = item["best_available_link"]
            if link in seen_links:
                continue
            listings.append(item)
            seen_links.add(link)

    listings.sort(
        key=lambda item: (
            item["displayed_price_cny"] is not None,
            bool(item["general_promotions"]),
            item["product_url"] is not None,
            item["freshness"] == "recent_index_entry",
        ),
        reverse=True,
    )
    direct_product_links = [
        {
            "title": item["title"],
            "product_url": item["product_url"],
            "product_id": item["product_id"],
        }
        for item in listings
        if item["product_url"]
    ][:limit]
    listings = listings[:limit]
    status = "ok" if listings else ("index_unavailable" if errors else "no_results")
    payload = {
        "platform": platform,
        "platform_name": config["display_name"],
        "status": status,
        "observed_at": _now_iso(),
        "source": "360 Search public web index",
        "official_search_url": _build_one_search_link(keyword, platform),
        "direct_product_links": direct_product_links,
        "listings": listings,
        "errors": errors,
        "cache_hit": False,
        "note": (
            "无需登录；价格和优惠来自公开索引，可能滞后或对应不同 SKU。"
            "拼多多通常不公开索引商品直链，因此可能只返回促销来源链接和官方搜索入口。"
        ),
    }
    _cache[cache_key] = (time.monotonic(), payload)
    return payload


server = FastMCP(
    SERVER_NAME,
    instructions=(
        "Login-free, read-only product discovery for Pinduoduo, JD, and Taobao. "
        "Return public index prices, general promotions, direct product/source links, "
        "and official search links. Public index values are not checkout prices; "
        "always preserve platform, SKU, observation time, freshness, and confidence."
    ),
    stateless_http=True,
)

READ_ONLY_OPEN = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


@server.tool(
    name="search_products",
    title="免登录搜索中国电商商品",
    description=(
        "无需登录，搜索拼多多、京东和淘宝的公开商品/促销索引，返回观察价格、"
        "一般优惠、商品或促销来源链接，以及平台官方搜索入口。platform 可选 "
        "pdd、jd、taobao、all；默认 all。价格不是结算页实付价。"
    ),
    annotations=READ_ONLY_OPEN,
    structured_output=True,
)
async def search_products(
    keyword: str,
    platform: str = "all",
    limit: int = 5,
    max_price_cny: float | None = None,
) -> dict[str, Any]:
    normalized_keyword = _validate_keyword(keyword)
    normalized_platform = _validate_platform(platform, allow_all=True)
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    if max_price_cny is not None and not 0 < max_price_cny <= 1_000_000:
        raise ValueError("max_price_cny must be greater than 0")

    selected = list(PLATFORMS) if normalized_platform == "all" else [normalized_platform]
    searches = [
        await _search_one_platform(normalized_keyword, name, limit, max_price_cny)
        for name in selected
    ]
    return {
        "keyword": normalized_keyword,
        "requested_platform": normalized_platform,
        "max_price_cny": max_price_cny,
        "observed_at": _now_iso(),
        "searches": searches,
        "login_required": False,
        "verification_required": True,
        "verification_note": (
            "公开索引价可能过期、混合规格或缺少店铺券；请在商品页确认准确 SKU，"
            "并以订单确认页为最终价格。"
        ),
    }


@server.tool(
    name="build_search_links",
    title="生成三平台官方搜索链接",
    description=(
        "为关键词生成拼多多、京东和淘宝官方搜索链接；不访问网络，也不需要登录。"
    ),
    annotations=READ_ONLY_LOCAL,
    structured_output=True,
)
def build_search_links(
    keyword: str, platforms: list[str] | None = None
) -> dict[str, Any]:
    normalized_keyword = _validate_keyword(keyword)
    selected = platforms or list(PLATFORMS)
    normalized_platforms: list[str] = []
    for platform in selected:
        name = _validate_platform(platform)
        if name not in normalized_platforms:
            normalized_platforms.append(name)
    return {
        "keyword": normalized_keyword,
        "login_required": False,
        "links": [
            {
                "platform": platform,
                "platform_name": PLATFORMS[platform]["display_name"],
                "url": _build_one_search_link(normalized_keyword, platform),
            }
            for platform in normalized_platforms
        ],
    }


@server.tool(
    name="parse_product_link",
    title="解析三平台商品链接",
    description="识别拼多多、京东或淘宝商品链接并提取商品 ID；不访问网络。",
    annotations=READ_ONLY_LOCAL,
    structured_output=True,
)
def parse_product_link(url: str) -> dict[str, Any]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {
            "recognized": False,
            "platform": None,
            "product_id": None,
            "url": url,
            "reason": "A valid http(s) URL is required.",
        }

    hostname = parsed.hostname.lower()
    query = parse_qs(parsed.query)
    platform: str | None = None
    product_id: str | None = None
    if _hostname_matches(hostname, PLATFORMS["pdd"]["host_suffixes"]):
        platform = "pdd"
        product_id = (query.get("goods_id") or [None])[0]
    elif _hostname_matches(hostname, PLATFORMS["jd"]["host_suffixes"]):
        platform = "jd"
        match = re.search(r"/(\d+)\.html(?:$|[?#])", url)
        product_id = match.group(1) if match else None
    elif _hostname_matches(hostname, PLATFORMS["taobao"]["host_suffixes"]):
        platform = "taobao"
        product_id = (query.get("id") or [None])[0]

    return {
        "recognized": platform is not None,
        "platform": platform,
        "platform_name": PLATFORMS[platform]["display_name"] if platform else None,
        "product_id": product_id,
        "url": url,
        "reason": None if platform else "Unsupported marketplace hostname.",
    }


async def _live_test(keyword: str) -> int:
    payload = await search_products(keyword=keyword, platform="all", limit=3)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if any(search["listings"] for search in payload["searches"]) else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Login-free Chinese e-commerce MCP")
    parser.add_argument(
        "--live-test",
        metavar="KEYWORD",
        help="Run a live public-index search instead of starting the MCP server.",
    )
    args = parser.parse_args()
    if args.live_test:
        raise SystemExit(asyncio.run(_live_test(args.live_test)))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
