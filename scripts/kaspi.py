#!/usr/bin/env python3
"""Small, dependency-free CLI for polite Kaspi product research."""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SEARCH_URL = "https://kaspi.kz/yml/product-view/pl/results"
OFFER_URL = "https://kaspi.kz/yml/offer-view/offers"
SHORTEN_URL = "https://kaspi.kz/shop/u/shorten"
SHOP_ROOT = "https://kaspi.kz/shop"
QR_URL = "https://quickchart.io/qr"
DEFAULT_CITY_CODE = "750000000"
DEFAULT_ZONE = "Magnum_ZONE1"
DEFAULT_CITY_NAME = "Алматы"
DEFAULT_TIMEZONE = "Asia/Almaty"
RUSSIAN_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
STOPWORDS = {
    "и",
    "в",
    "на",
    "для",
    "с",
    "по",
    "из",
    "шт",
    "штук",
    "купить",
    "товар",
    "kaspi",
    "каспи",
    "kz",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_html(value: str | None) -> str | None:
    if not value:
        return None
    parser = _TextExtractor()
    parser.feed(html_lib.unescape(value))
    cleaned = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return cleaned or None


def _decode_js_string(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except json.JSONDecodeError:
        return html_lib.unescape(value.replace(r"\u002F", "/"))


def _request_text(
    url: str,
    referer: str | None,
    timeout: float,
    *,
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Kaspi returned HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Kaspi for {url}: {exc.reason}") from exc


def _request_json(
    url: str,
    referer: str | None,
    timeout: float,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json, text/*"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if extra_headers:
        headers.update(extra_headers)
    raw = _request_text(
        url,
        referer,
        timeout,
        method=method,
        body=body,
        extra_headers=headers,
    )
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected a JSON object from {url}")
    return parsed


def _is_kaspi_url(parts: Any) -> bool:
    hostname = (parts.hostname or "").lower()
    return parts.scheme in {"http", "https"} and (
        hostname == "kaspi.kz" or hostname.endswith(".kaspi.kz")
    )


def _absolute_shop_link(shop_link: str | None, city_code: str) -> str | None:
    if not shop_link:
        return None
    url = shop_link if shop_link.startswith("http") else SHOP_ROOT + shop_link
    parts = urlsplit(url)
    if not _is_kaspi_url(parts):
        return None
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("c", city_code)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _public_product_url(url: str | None, city_code: str) -> str | None:
    """Return a shareable Kaspi URL without session or tracking parameters."""
    if not url:
        return None
    parts = urlsplit(url)
    if not _is_kaspi_url(parts):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"c": city_code}), ""))


def _qr_code_url(product_url: str | None) -> str | None:
    if not product_url:
        return None
    return f"{QR_URL}?size=140&margin=1&text={quote(product_url, safe='')}"


def _request_qr_png(url: str, timeout: float) -> bytes:
    """Legacy URL-QR fallback. Official Kaspi QR capture is the default."""
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/png,image/*;q=0.9,*/*;q=0.5",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"QR service returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach QR service: {exc.reason}") from exc
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("QR service did not return a PNG image")
    return data


def _is_png(path: Path) -> bool:
    try:
        return path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _official_qr_target(product_url: str, city_code: str, timeout: float) -> str:
    parts = urlsplit(product_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["c"] = city_code
    query["referrer"] = "desktop_QR"
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    endpoint = SHORTEN_URL + "?" + urlencode({"url": target})
    short_url = _request_text(
        endpoint,
        product_url,
        timeout,
        extra_headers={"Accept": "application/json, text/*", "X-KS-City": city_code},
    ).strip().strip('"')
    if not short_url.startswith("https://l.kaspi.kz/"):
        raise RuntimeError("Kaspi did not return an official app short-link")
    return short_url


def _run_agent_browser(session: str, *arguments: str, timeout: float) -> str:
    executable = shutil.which("agent-browser")
    if not executable:
        raise RuntimeError("agent-browser is required to capture the official Kaspi QR")
    command = [executable, "--session", session, *arguments]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(5.0, timeout),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Official Kaspi QR capture timed out: {' '.join(arguments)}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Official Kaspi QR capture failed: {message[:400]}")
    return result.stdout.strip()


def _qr_output_directory(output_dir: str | None) -> Path:
    """Resolve the QR cache lazily so read-only agents can still import the CLI."""
    try:
        directory = (
            Path(output_dir).expanduser()
            if output_dir
            else Path(tempfile.gettempdir()) / "kaspi-qr"
        )
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "Could not create the QR output directory; pass --qr-output-dir "
            "with a writable path or use --qr-mode none"
        ) from exc
    return directory


def _capture_official_qr(
    item: dict[str, Any],
    output_dir: str | None = None,
    timeout: float = 25.0,
) -> str | None:
    product_url = _public_product_url(item.get("link") or item.get("url"), item.get("cityCode") or DEFAULT_CITY_CODE)
    if not product_url:
        return None
    city_code = str(item.get("cityCode") or dict(parse_qsl(urlsplit(product_url).query)).get("c") or DEFAULT_CITY_CODE)
    directory = _qr_output_directory(output_dir)
    digest = hashlib.sha256((product_url + "|official-app-qr-v2").encode("utf-8")).hexdigest()[:20]
    path = (directory / f"kaspi-official-{digest}.png").absolute()
    short_url = _official_qr_target(product_url, city_code, timeout)

    if not _is_png(path):
        session = f"kaspi-qr-{os.getpid()}-{digest[:8]}"
        try:
            _run_agent_browser(
                session,
                "cookies",
                "set",
                "kaspi.storefront.cookie.city",
                city_code,
                "--url",
                "https://kaspi.kz",
                timeout=timeout,
            )
            _run_agent_browser(session, "open", product_url, timeout=timeout)
            _run_agent_browser(
                session,
                "wait",
                "--text",
                "Открыть в приложении Kaspi.kz",
                timeout=timeout,
            )
            # The button is server-rendered before its React click handler is hydrated.
            _run_agent_browser(session, "wait", "700", timeout=timeout)
            _run_agent_browser(
                session,
                "find",
                "role",
                "button",
                "click",
                "--name",
                "Открыть в приложении Kaspi.kz",
                "--exact",
                timeout=timeout,
            )
            _run_agent_browser(
                session,
                "wait",
                "--text",
                "Сканируйте, чтобы перейти",
                timeout=timeout,
            )
            _run_agent_browser(
                session,
                "wait",
                "--fn",
                "(()=>{const c=document.querySelector('.product-qr__canvas-wrapper canvas');"
                "return !!c&&c.width>=160&&c.height>=160})()",
                timeout=timeout,
            )
            _run_agent_browser(session, "wait", "700", timeout=timeout)
            _run_agent_browser(
                session,
                "eval",
                "(()=>{const e=document.querySelector('.product-qr__canvas-wrapper');"
                "if(!e)throw new Error('Kaspi QR wrapper not found');"
                "e.style.width='160px';e.style.height='160px';e.style.margin='0';return true})()",
                timeout=timeout,
            )
            _run_agent_browser(
                session,
                "screenshot",
                ".product-qr__canvas-wrapper",
                str(path),
                timeout=timeout,
            )
        finally:
            try:
                _run_agent_browser(session, "close", timeout=10.0)
            except RuntimeError:
                pass
    if not _is_png(path):
        raise RuntimeError("Kaspi QR screenshot was not written as PNG")
    item["qrLocalPath"] = str(path)
    item["qrMarkdown"] = f"![QR]({path})"
    item["qrKind"] = "kaspi_official_app"
    item["qrTargetUrl"] = short_url
    item["qrEvidence"] = "kaspi_product_modal"
    return str(path)


def _materialize_local_qr(
    item: dict[str, Any],
    output_dir: str | None = None,
    timeout: float = 20.0,
    qr_fetcher: Callable[[str, float], bytes] | None = None,
) -> str | None:
    if qr_fetcher is None:
        return _capture_official_qr(item, output_dir, timeout)

    # Explicit legacy/test fallback: encode the public web URL without Kaspi app branding.
    product_url = item.get("link") or item.get("url")
    qr_url = _qr_code_url(product_url)
    if not product_url or not qr_url:
        return None

    directory = _qr_output_directory(output_dir)
    digest = hashlib.sha256(product_url.encode("utf-8")).hexdigest()[:20]
    path = (directory / f"kaspi-{digest}.png").absolute()
    if not path.exists() or not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        path.write_bytes(qr_fetcher(qr_url, timeout))

    item["qrLocalPath"] = str(path)
    item["qrMarkdown"] = f"![QR]({path})"
    item["qrKind"] = "public_web_url_fallback"
    item["qrEvidence"] = "explicit_fallback"
    return str(path)


def _delivery_info(raw_value: Any) -> dict[str, Any]:
    value = str(raw_value or "").strip().upper() or None
    if value in {"EXPRESS", "TODAY"}:
        group = "today"
        label = "Экспресс / сегодня*" if value == "EXPRESS" else "Сегодня*"
    elif value == "TOMORROW":
        group = "tomorrow"
        label = "Завтра*"
    elif value and (match := re.fullmatch(r"TILL_(\d+)_DAYS", value)):
        days = int(match.group(1))
        group = "later"
        label = f"До {days} дн.*"
    elif value == "OTHER":
        group = "later"
        label = "Позже / срок в карточке*"
    else:
        group = "unknown"
        label = "Срок не подтвержден"
    return {
        "deliveryDuration": value,
        "deliveryGroup": group,
        "deliveryLabel": label,
        "isFastDelivery": group in {"today", "tomorrow"},
    }


def _normalize_item(item: dict[str, Any], query: str, city_code: str) -> dict[str, Any]:
    link = _absolute_shop_link(item.get("shopLink"), city_code)
    public_link = _public_product_url(link, city_code)
    normalized = {
        "id": item.get("id") or item.get("configSku"),
        "configSku": item.get("configSku"),
        "title": item.get("title"),
        "brand": item.get("brand"),
        "price": item.get("priceFormatted"),
        "unitPrice": item.get("unitPrice"),
        "rating": item.get("rating"),
        "reviews": item.get("reviewsQuantity") or item.get("reviews"),
        "stock": item.get("stock"),
        "link": public_link or link,
        "cityCode": city_code,
        "categoryId": item.get("categoryId"),
        "category": item.get("category") or item.get("categoryRu") or [],
        "categoryCodes": item.get("categoryCodes") or [],
        "baseProductCodes": item.get("baseProductCodes") or [],
        "bestMerchant": item.get("bestMerchant"),
        "isBrandOfficialPartner": bool(item.get("isBrandOfficialPartner")),
        "qrLocalPath": None,
        "qrMarkdown": None,
        "deliveryZones": item.get("deliveryZones") or [],
        "matchedQueries": [query],
    }
    normalized.update(_delivery_info(item.get("deliveryDuration")))
    return normalized


def _normalize_words(value: Any) -> list[str]:
    return [
        token
        for token in re.findall(r"[0-9a-zа-яё]+", str(value or "").lower())
        if token not in STOPWORDS and len(token) > 1
    ]


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").lower())


def _searchable_text(item: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("title", "brand", "categoryId"):
        if item.get(key):
            values.append(str(item[key]))
    for key in ("category", "categoryCodes", "baseProductCodes"):
        values.extend(str(value) for value in item.get(key, []) if value)
    return " ".join(values).lower()


def _matches_group(haystack: str, expression: str) -> bool:
    compact_haystack = _compact(haystack)
    for option in expression.split("|"):
        normalized = option.strip().lower()
        if not normalized:
            continue
        if normalized in haystack or _compact(normalized) in compact_haystack:
            return True
    return False


def _query_score(item: dict[str, Any], query: str) -> tuple[float, list[str]]:
    haystack = _searchable_text(item)
    haystack_tokens = set(_normalize_words(haystack))
    compact_haystack = _compact(haystack)
    query_tokens = _normalize_words(query)
    if not query_tokens:
        return 0.0, []

    matched: list[str] = []
    matched_weight = 0.0
    total_weight = 0.0
    model_hit = False
    for token in query_tokens:
        is_model = any(char.isdigit() for char in token) and len(token) >= 3
        weight = 3.0 if is_model else 1.0
        total_weight += weight
        found = token in haystack_tokens or (is_model and _compact(token) in compact_haystack)
        if found:
            matched.append(token)
            matched_weight += weight
            model_hit = model_hit or is_model

    score = 75.0 * matched_weight / max(1.0, total_weight)
    brand = str(item.get("brand") or "").strip().lower()
    if brand and brand not in {"без бренда", "no name"} and brand in query.lower():
        score += 10.0
    if model_hit:
        score += 15.0
    normalized_query = " ".join(_normalize_words(query))
    normalized_title = " ".join(_normalize_words(item.get("title")))
    if normalized_query and normalized_query in normalized_title:
        score += 10.0
    score += min(10.0, 4.0 * max(0, len(item.get("matchedQueries", [])) - 1))
    return min(100.0, round(score, 2)), matched


def _rank_and_filter_items(
    items: list[dict[str, Any]],
    queries: list[str],
    required_terms: list[str],
    required_materials: list[str],
    excluded_terms: list[str],
    min_relevance: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in items:
        haystack = _searchable_text(item)
        reasons: list[str] = []
        if required_terms and not all(_matches_group(haystack, term) for term in required_terms):
            reasons.append("не выполнены обязательные признаки")
        if required_materials and not all(
            _matches_group(haystack, material) for material in required_materials
        ):
            reasons.append("не совпадает требуемый материал")
        if any(_matches_group(haystack, term) for term in excluded_terms):
            reasons.append("совпало исключающее слово")

        scored = [_query_score(item, query) for query in queries]
        best_score, matched_tokens = max(scored, default=(0.0, []), key=lambda value: value[0])
        item["relevanceScore"] = best_score
        item["matchedTokens"] = matched_tokens
        item["relevanceEvidence"] = "title_brand_category_model_token_overlap"
        if best_score < min_relevance:
            reasons.append(f"низкая релевантность ({best_score:g})")
        if reasons:
            item["rejectionReasons"] = reasons
            rejected.append(item)
        else:
            accepted.append(item)

    delivery_order = {"today": 0, "tomorrow": 1, "later": 2, "unknown": 3}
    accepted.sort(
        key=lambda item: (
            -float(item.get("relevanceScore") or 0),
            delivery_order.get(str(item.get("deliveryGroup")), 3),
            -math.log1p(float(item.get("reviews") or 0)),
            float(item.get("unitPrice") or math.inf),
        )
    )
    return accepted, rejected


def _canonical_model_key(item: dict[str, Any]) -> str:
    source = " ".join(str(value) for value in item.get("baseProductCodes", []))
    candidates = re.findall(r"(?<![0-9a-zа-яё])(?=[0-9a-zа-яё.\-]{3,})(?:[a-zа-яё]*\d[0-9a-zа-яё.\-]*)", source.lower())
    item_ids = {_compact(item.get("id")), _compact(item.get("configSku"))}
    models = [
        _compact(candidate)
        for candidate in candidates
        if len(_compact(candidate)) >= 3 and _compact(candidate) not in item_ids
    ]
    model = max(models, key=len, default="")
    if not model:
        return f"id:{item.get('id') or item.get('configSku') or _compact(item.get('title'))}"
    brand = _compact(item.get("brand") or "unknown")
    category = _compact(item.get("categoryId") or (item.get("categoryCodes") or [""])[0])
    return f"{brand}:{category}:{model}"


def _listing_preference(item: dict[str, Any]) -> tuple[int, float, float]:
    delivery_order = {"today": 0, "tomorrow": 1, "later": 2, "unknown": 3}
    return (
        delivery_order.get(str(item.get("deliveryGroup")), 3),
        -float(item.get("relevanceScore") or 0),
        float(item.get("unitPrice") or math.inf),
    )


def _dedupe_model_variants(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        key = _canonical_model_key(item)
        item["canonicalModelKey"] = key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    deduped: list[dict[str, Any]] = []
    for key in order:
        variants = sorted(groups[key], key=_listing_preference)
        winner = variants[0]
        queries: list[str] = []
        for variant in variants:
            for query in variant.get("matchedQueries", []):
                if query not in queries:
                    queries.append(query)
        winner["matchedQueries"] = queries
        winner["listingVariants"] = [
            {
                "id": variant.get("id"),
                "link": variant.get("link"),
                "price": variant.get("price"),
                "unitPrice": variant.get("unitPrice"),
                "deliveryLabel": variant.get("deliveryLabel"),
                "deliveryGroup": variant.get("deliveryGroup"),
            }
            for variant in variants
        ]
        winner["canonicalModelKey"] = key
        deduped.append(winner)
    deduped.sort(key=lambda item: (-float(item.get("relevanceScore") or 0), _listing_preference(item)))
    return deduped


def _item_key(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("configSku") or item.get("link") or item.get("title"))


def _partition_by_delivery(
    items: list[dict[str, Any]], delivery_window: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    primary: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    accepted = {
        "today": {"today"},
        "tomorrow": {"tomorrow"},
        "fast": {"today", "tomorrow"},
        "any": {"today", "tomorrow", "later"},
    }[delivery_window]

    for item in items:
        group = item.get("deliveryGroup") or "unknown"
        if group in accepted:
            primary.append(item)
        elif group == "unknown":
            unknown.append(item)
        else:
            later.append(item)
    return primary, later, unknown


def search(args: argparse.Namespace) -> dict[str, Any]:
    queries = list(dict.fromkeys(q.strip() for q in args.query if q.strip()))
    if not queries:
        raise RuntimeError("Provide at least one non-empty --query")
    if len(queries) > 6:
        raise RuntimeError("Use at most six query variants per run")

    results: dict[str, dict[str, Any]] = {}
    per_query: list[dict[str, Any]] = []
    zone = None if args.no_zone else args.zone

    for index, query in enumerate(queries):
        params = {
            "text": query,
            "c": args.city_code,
            "sort": args.sort,
            "page": str(args.page),
        }
        if zone:
            params["q"] = f":availableInZones:{zone}"
        url = SEARCH_URL + "?" + urlencode(params)
        referer = f"https://kaspi.kz/shop/search/?text={quote(query)}&c={quote(args.city_code)}"
        payload = json.loads(_request_text(url, referer, args.timeout))
        raw_items = payload.get("data", [])
        if not isinstance(raw_items, list):
            raise RuntimeError(f"Unexpected search response for query: {query}")

        selected = raw_items[: args.limit]
        per_query.append({"query": query, "returned": len(raw_items), "kept": len(selected)})
        for raw_item in selected:
            if not isinstance(raw_item, dict):
                continue
            item = _normalize_item(raw_item, query, args.city_code)
            key = _item_key(item)
            if key in results:
                if query not in results[key]["matchedQueries"]:
                    results[key]["matchedQueries"].append(query)
            else:
                results[key] = item

        if index + 1 < len(queries) and args.delay:
            time.sleep(args.delay)

    raw_deduped_items = list(results.values())
    items, rejected = _rank_and_filter_items(
        raw_deduped_items,
        queries,
        list(getattr(args, "require_term", []) or []),
        list(getattr(args, "require_material", []) or []),
        list(getattr(args, "exclude_term", []) or []),
        float(getattr(args, "min_relevance", 20.0)),
    )
    items = _dedupe_model_variants(items)
    for source_rank, item in enumerate(items, start=1):
        item["sourceRank"] = source_rank

    primary, later, unknown = _partition_by_delivery(items, args.delivery_window)
    ordered_items = primary + later + unknown
    for rank, item in enumerate(ordered_items, start=1):
        item["rank"] = rank

    return {
        "source": "Kaspi live product search",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "cityCode": args.city_code,
        "cityName": getattr(args, "city_name", None),
        "zone": zone,
        "timezone": getattr(args, "timezone", DEFAULT_TIMEZONE),
        "requestedDeliveryWindow": args.delivery_window,
        "queries": per_query,
        "uniqueItems": len(ordered_items),
        "deliverySummary": {
            "primary": len(primary),
            "later": len(later),
            "unknown": len(unknown),
        },
        "primaryItems": primary,
        "laterItems": later,
        "unverifiedDeliveryItems": unknown,
        "rejectedItems": rejected,
        "items": ordered_items,
        "deliveryCaveat": (
            "* Срок взят из поисковой карточки Kaspi для выбранного города/зоны; "
            "точное обещание зависит от адреса, продавца, времени заказа и подтверждается при оформлении."
        ),
        "qrPrivacy": (
            "QR снимается из официальной модалки Kaspi и ведёт на l.kaspi.kz app-link; "
            "точный адрес, cookie и параметры сессии в QR не добавляются."
        ),
    }


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_json_ld(page: str) -> dict[str, Any] | None:
    blocks = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        page,
        flags=re.I | re.S,
    )
    for block in blocks:
        try:
            parsed = json.loads(html_lib.unescape(block.strip()))
        except json.JSONDecodeError:
            continue
        for candidate in _walk_json(parsed):
            kind = candidate.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" in kinds:
                return candidate
    return None


def _extract_description(page: str, product: dict[str, Any] | None) -> str | None:
    if product:
        cleaned = _clean_html(product.get("description"))
        if cleaned:
            return cleaned
    match = re.search(r'"description"\s*:\s*"(.*?)"\s*,\s*"image"', page, re.S)
    if not match:
        return None
    return _clean_html(_decode_js_string(match.group(1)))


def _extract_specs(page: str) -> list[dict[str, str]]:
    start = page.find('"specifications"')
    if start >= 0:
        array_start = page.find("[", start)
        if array_start >= 0:
            try:
                groups, _ = json.JSONDecoder().raw_decode(page[array_start:])
            except json.JSONDecodeError:
                groups = None
            if isinstance(groups, list):
                specs: list[dict[str, str]] = []
                for group in groups:
                    if not isinstance(group, dict):
                        continue
                    group_name = str(group.get("name") or "").strip()
                    features = group.get("features")
                    if not isinstance(features, list):
                        continue
                    for feature in features:
                        if not isinstance(feature, dict):
                            continue
                        name = str(feature.get("name") or "").strip()
                        raw_values = feature.get("featureValues")
                        if not name or not isinstance(raw_values, list):
                            continue
                        values = [
                            str(item.get("value")).strip()
                            for item in raw_values
                            if isinstance(item, dict) and item.get("value") is not None
                        ]
                        if values:
                            specs.append(
                                {
                                    "group": group_name,
                                    "name": name,
                                    "value": ", ".join(values),
                                }
                            )
                if specs:
                    return specs[:120]

    region = page[start : start + 150_000] if start >= 0 else page
    pattern = re.compile(
        r'"name"\s*:\s*"((?:\\.|[^"\\])*)".*?'
        r'"featureValues"\s*:\s*\[\s*\{.*?'
        r'"value"\s*:\s*"((?:\\.|[^"\\])*)"',
        re.S,
    )
    specs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in pattern.finditer(region):
        name = _decode_js_string(match.group(1)).strip()
        value = _decode_js_string(match.group(2)).strip()
        pair = (name, value)
        if name and value and pair not in seen:
            specs.append({"name": name, "value": value})
            seen.add(pair)
    return specs[:120]


def _offer_summary(product: dict[str, Any] | None) -> dict[str, Any] | None:
    if not product:
        return None
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    return {
        key: offers.get(key)
        for key in ("price", "lowPrice", "highPrice", "priceCurrency", "availability")
        if offers.get(key) is not None
    } or None


def _extract_first_json_value(page: str, key: str) -> Any:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*')
    for match in pattern.finditer(page):
        try:
            value, _ = json.JSONDecoder().raw_decode(page[match.end() :])
        except json.JSONDecodeError:
            continue
        return value
    return None


def _extract_product_context(
    page: str, product: dict[str, Any] | None
) -> dict[str, Any]:
    brand_value = product.get("brand") if product else None
    if isinstance(brand_value, dict):
        brand_value = brand_value.get("name")
    category_codes = _extract_first_json_value(page, "categoryCodes")
    base_codes = _extract_first_json_value(page, "baseProductCodes")
    return {
        "brand": brand_value,
        "categoryCodes": category_codes if isinstance(category_codes, list) else [],
        "baseProductCodes": base_codes if isinstance(base_codes, list) else [],
        "groups": None,
        "productSeries": [],
    }


def _spec_map(specifications: list[dict[str, str]]) -> dict[str, str]:
    return {
        str(spec.get("name") or "").strip().lower(): str(spec.get("value") or "").strip()
        for spec in specifications
        if spec.get("name") and spec.get("value")
    }


def _detect_conflicts(description: str | None, specifications: list[dict[str, str]]) -> list[str]:
    warnings: list[str] = []
    description_text = str(description or "").lower()
    specs = _spec_map(specifications)
    length_value = specs.get("длина")
    if length_value:
        spec_match = re.search(r"\d+(?:[.,]\d+)?", length_value)
        ranges = re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:-|–|—|до)\s*(\d+(?:[.,]\d+)?)\s*см", description_text)
        if spec_match and ranges:
            spec_number = float(spec_match.group(0).replace(",", "."))
            if not any(
                float(low.replace(",", ".")) <= spec_number <= float(high.replace(",", "."))
                for low, high in ranges
            ):
                warnings.append(
                    f"Длина расходится: характеристики {length_value}, описание {ranges[0][0]}–{ranges[0][1]} см"
                )
    material = specs.get("материал", "").lower()
    precise_materials = [
        name for name in ("бук", "береза", "берёза", "тик", "дуб", "бамбук") if name in description_text
    ]
    if material in {"дерево", "древесина"} and precise_materials:
        warnings.append(
            f"Материал указан неравномерно: в характеристиках «{material}», в описании «{precise_materials[0]}»"
        )
    return warnings


def _parse_datetime(value: Any, local_timezone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(local_timezone)


def _money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.0f} ₸".replace(",", " ")


def _format_delivery_datetime(
    instant: datetime | None, delivery_type: str | None, cost: Any
) -> str:
    kind = {
        "EXPRESS": "Express",
        "TO_DOOR": "Доставка",
        "PICKUP": "Самовывоз",
    }.get(str(delivery_type or "").upper(), "Доставка")
    when = "срок не указан"
    if instant:
        when = f"{instant.day} {RUSSIAN_MONTHS[instant.month]} до {instant:%H:%M}"
    cost_label = _money(cost) if cost not in (None, 0, 0.0) else "бесплатно"
    return f"{kind}, {when}, {cost_label}*"


def _absolute_delivery_info(
    delivery_date: str | None,
    local_now: datetime,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    if not delivery_date:
        return fallback
    try:
        promised_date = datetime.fromisoformat(delivery_date).date()
    except ValueError:
        return fallback
    delta = (promised_date - local_now.date()).days
    if delta <= 0:
        group = "today"
    elif delta == 1:
        group = "tomorrow"
    else:
        group = "later"
    resolved = dict(fallback)
    resolved.update(
        {
            "deliveryGroup": group,
            "isFastDelivery": group in {"today", "tomorrow"},
            "deliveryDateEvidence": "seller_absolute_date",
        }
    )
    return resolved


def _normalize_offers(
    payload: dict[str, Any],
    local_timezone: ZoneInfo,
    local_now: datetime | None = None,
) -> list[dict[str, Any]]:
    local_now = local_now or datetime.now(local_timezone)
    normalized: list[dict[str, Any]] = []
    for raw_offer in payload.get("offers", []):
        if not isinstance(raw_offer, dict):
            continue
        raw_options = raw_offer.get("deliveryOptions")
        options: list[dict[str, Any]] = []
        if isinstance(raw_options, dict):
            for option_name, raw_option in raw_options.items():
                if not isinstance(raw_option, dict):
                    continue
                option_type = str(raw_option.get("deliveryType") or option_name).upper()
                instant = _parse_datetime(raw_option.get("delivery"), local_timezone)
                options.append(
                    {
                        "deliveryType": option_type,
                        "deliveryAt": instant.isoformat() if instant else None,
                        "deliveryDate": instant.date().isoformat() if instant else None,
                        "deliveryCost": raw_option.get("deliveryCost", raw_option.get("cost")),
                        "deliveryLabel": _format_delivery_datetime(
                            instant,
                            option_type,
                            raw_option.get("deliveryCost", raw_option.get("cost")),
                        ),
                        "description": raw_option.get("description")
                        if isinstance(raw_option.get("description"), dict)
                        else None,
                    }
                )
        preferred_type = str(raw_offer.get("deliveryType") or "").upper()
        chosen = next((option for option in options if option["deliveryType"] == preferred_type), None)
        if chosen is None:
            chosen = next((option for option in options if option["deliveryType"] == "EXPRESS"), None)
        if chosen is None:
            chosen = next((option for option in options if option["deliveryType"] == "TO_DOOR"), None)
        if chosen is None and options:
            chosen = options[0]
        delivery = _delivery_info(raw_offer.get("deliveryDuration"))
        delivery = _absolute_delivery_info(
            chosen.get("deliveryDate") if chosen else None,
            local_now,
            delivery,
        )
        normalized_offer = {
            "merchantId": raw_offer.get("merchantId"),
            "merchantName": raw_offer.get("merchantName"),
            "merchantSku": raw_offer.get("merchantSku"),
            "merchantRating": raw_offer.get("merchantRating"),
            "merchantReviews": raw_offer.get("merchantReviewsQuantity"),
            "isBrandOfficialPartner": bool(raw_offer.get("isBrandOfficialPartner")),
            "price": raw_offer.get("price"),
            "priceFormatted": _money(raw_offer.get("price")),
            "deliveryOptions": options,
            "deliveryEvidence": "seller_offers_api",
            "priceEvidence": "seller_offers_api",
        }
        normalized_offer.update(delivery)
        if chosen:
            normalized_offer.update(
                {
                    "deliveryType": chosen.get("deliveryType"),
                    "deliveryAt": chosen.get("deliveryAt"),
                    "deliveryDate": chosen.get("deliveryDate"),
                    "deliveryCost": chosen.get("deliveryCost"),
                    "deliveryLabel": chosen.get("deliveryLabel"),
                    "deliveryDescription": chosen.get("description"),
                }
            )
        normalized.append(normalized_offer)
    return normalized


def _select_offer(offers: list[dict[str, Any]], delivery_window: str) -> dict[str, Any] | None:
    accepted = {
        "today": {"today"},
        "tomorrow": {"tomorrow"},
        "fast": {"today", "tomorrow"},
        "any": {"today", "tomorrow", "later"},
    }[delivery_window]
    eligible = [offer for offer in offers if offer.get("deliveryGroup") in accepted]
    pool = eligible or offers
    if not pool:
        return None
    group_order = {"today": 0, "tomorrow": 1, "later": 2, "unknown": 3}
    return min(
        pool,
        key=lambda offer: (
            group_order.get(str(offer.get("deliveryGroup")), 3),
            float(offer.get("price") or math.inf),
            -float(offer.get("merchantRating") or 0),
        ),
    )


def _offer_payload(
    product_id: str,
    city_code: str,
    zone: str | None,
    context: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cityId": city_code,
        "id": product_id,
        "merchantUID": [],
        "limit": limit,
        "page": 0,
        "product": {
            "brand": context.get("brand"),
            "categoryCodes": context.get("categoryCodes") or [],
            "baseProductCodes": context.get("baseProductCodes") or [],
            "groups": context.get("groups"),
            "productSeries": context.get("productSeries") or [],
        },
        "sortOption": "PRICE",
        "highRating": None,
        "searchText": None,
        "isExcellentMerchant": False,
        "installationId": "-1",
    }
    if zone:
        payload["zoneId"] = [zone]
    return payload


def _fetch_offers(
    product_id: str,
    product_url: str,
    city_code: str,
    zone: str | None,
    context: dict[str, Any],
    limit: int,
    timeout: float,
    local_timezone: ZoneInfo,
) -> list[dict[str, Any]]:
    response = _request_json(
        f"{OFFER_URL}/{quote(product_id)}",
        product_url,
        timeout,
        method="POST",
        payload=_offer_payload(product_id, city_code, zone, context, limit),
        extra_headers={"X-KS-City": city_code},
    )
    return _normalize_offers(response, local_timezone)


def _key_parameters(specifications: list[dict[str, str]], limit: int = 4) -> list[str]:
    priority = (
        "материал",
        "тип",
        "длина",
        "ширина",
        "размер",
        "особенности",
        "мощность",
        "объем",
        "объём",
        "совместимость",
        "комплектация",
    )
    mapped = _spec_map(specifications)
    result: list[str] = []
    for name in priority:
        if name in mapped:
            result.append(f"{name.capitalize()}: {mapped[name]}")
        if len(result) >= limit:
            break
    if len(result) < limit:
        for spec in specifications:
            label = str(spec.get("name") or "").strip()
            value = str(spec.get("value") or "").strip()
            rendered = f"{label}: {value}"
            if label and value and rendered not in result:
                result.append(rendered)
            if len(result) >= limit:
                break
    return result


def _add_city_code(url: str, city_code: str) -> str:
    parts = urlsplit(url)
    if not _is_kaspi_url(parts):
        raise RuntimeError(f"Expected a kaspi.kz product URL, got: {url}")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("c", city_code)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _fetch_product_detail(raw_url: str, args: argparse.Namespace) -> dict[str, Any]:
    url = _add_city_code(raw_url, args.city_code)
    page = _request_text(url, "https://kaspi.kz/shop/", args.timeout)
    product = _extract_json_ld(page)
    title_match = re.search(r"<title>(.*?)</title>", page, re.I | re.S)
    title = _clean_html(title_match.group(1)) if title_match else None
    rating = product.get("aggregateRating") if product else None
    public_url = _public_product_url(url, args.city_code) or url
    specifications = _extract_specs(page)
    description = _extract_description(page, product)
    context = _extract_product_context(page, product)
    brand = product.get("brand") if product else None
    if isinstance(brand, dict):
        brand = brand.get("name")
    product_info: dict[str, Any] = {
        "url": public_url,
        "link": public_url,
        "cityCode": args.city_code,
        "zone": getattr(args, "zone", None),
        "title": product.get("name") if product and product.get("name") else title,
        "sku": str(product.get("sku")) if product and product.get("sku") is not None else None,
        "brand": brand,
        "offer": _offer_summary(product),
        "aggregateRating": rating if isinstance(rating, dict) else None,
        "description": description,
        "specifications": specifications,
        "keyParameters": _key_parameters(specifications),
        "productContext": context,
        "warnings": _detect_conflicts(description, specifications),
        "qrLocalPath": None,
        "qrMarkdown": None,
        "fieldEvidence": {
            "specifications": "embedded_product_data",
            "description": "product_page",
        },
    }
    if not getattr(args, "no_offers", False) and product_info.get("sku"):
        try:
            offers = _fetch_offers(
                product_info["sku"],
                public_url,
                args.city_code,
                getattr(args, "zone", None),
                context,
                int(getattr(args, "offers_limit", 20)),
                args.timeout,
                ZoneInfo(getattr(args, "timezone", DEFAULT_TIMEZONE)),
            )
            product_info["sellerOffers"] = offers
            selected = _select_offer(offers, getattr(args, "delivery_window", "fast"))
            product_info["selectedOffer"] = selected
            if selected:
                product_info["price"] = selected.get("priceFormatted")
                product_info["unitPrice"] = selected.get("price")
                for key in (
                    "deliveryDuration",
                    "deliveryGroup",
                    "deliveryLabel",
                    "isFastDelivery",
                    "deliveryType",
                    "deliveryAt",
                    "deliveryDate",
                    "deliveryCost",
                    "deliveryEvidence",
                ):
                    product_info[key] = selected.get(key)
                product_info["fieldEvidence"].update(
                    {"price": "seller_offers_api", "delivery": "seller_offers_api"}
                )
        except RuntimeError as exc:
            product_info["offerError"] = str(exc)
            product_info["warnings"].append("Не удалось проверить предложения продавцов")
    if not getattr(args, "no_local_qr", False) and getattr(args, "qr_mode", "official") != "none":
        try:
            if getattr(args, "qr_mode", "official") == "fallback":
                _materialize_local_qr(
                    product_info,
                    getattr(args, "qr_output_dir", None),
                    args.timeout,
                    _request_qr_png,
                )
            else:
                _materialize_local_qr(
                    product_info,
                    getattr(args, "qr_output_dir", None),
                    args.timeout,
                )
        except RuntimeError as exc:
            product_info["qrError"] = str(exc)
    return product_info


def details(args: argparse.Namespace) -> dict[str, Any]:
    urls = list(dict.fromkeys(args.url))
    if len(urls) > 6:
        raise RuntimeError("Inspect at most six detail pages per run")

    products: list[dict[str, Any]] = []
    for index, raw_url in enumerate(urls):
        products.append(_fetch_product_detail(raw_url, args))
        if index + 1 < len(urls) and args.delay:
            time.sleep(args.delay)

    return {
        "source": "Kaspi live product pages",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "cityCode": args.city_code,
        "cityName": getattr(args, "city_name", None),
        "zone": getattr(args, "zone", None),
        "timezone": getattr(args, "timezone", DEFAULT_TIMEZONE),
        "products": products,
    }


def _merge_search_and_detail(
    item: dict[str, Any], detail: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(item)
    for key in (
        "title",
        "brand",
        "description",
        "specifications",
        "keyParameters",
        "productContext",
        "sellerOffers",
        "selectedOffer",
        "warnings",
        "fieldEvidence",
        "qrLocalPath",
        "qrMarkdown",
        "qrKind",
        "qrTargetUrl",
        "qrEvidence",
        "qrError",
        "offerError",
    ):
        if key in detail and detail.get(key) not in (None, [], {}):
            merged[key] = detail[key]
    selected = detail.get("selectedOffer")
    if isinstance(selected, dict):
        merged["price"] = selected.get("priceFormatted") or merged.get("price")
        merged["unitPrice"] = selected.get("price") or merged.get("unitPrice")
        merged["merchantName"] = selected.get("merchantName")
        merged["merchantRating"] = selected.get("merchantRating")
        merged["merchantReviews"] = selected.get("merchantReviews")
        for key in (
            "deliveryDuration",
            "deliveryGroup",
            "deliveryLabel",
            "isFastDelivery",
            "deliveryType",
            "deliveryAt",
            "deliveryDate",
            "deliveryCost",
            "deliveryEvidence",
        ):
            if selected.get(key) is not None:
                merged[key] = selected[key]
    return merged


def _enriched_model_key(item: dict[str, Any]) -> str:
    specs = _spec_map(list(item.get("specifications") or []))
    type_value = _compact(specs.get("тип"))
    material = _compact(specs.get("материал"))
    length = _compact(specs.get("длина"))
    if type_value and material and length:
        brand = _compact(item.get("brand") or "unknown")
        title = _compact(item.get("title"))
        category = _compact(
            item.get("categoryId")
            or ((item.get("productContext") or {}).get("categoryCodes") or [""])[0]
        )
        return f"verified:{brand}:{category}:{title}:{type_value}:{material}:{length}"
    return str(item.get("canonicalModelKey") or _canonical_model_key(item))


def _dedupe_enriched_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        key = _enriched_model_key(item)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    result: list[dict[str, Any]] = []
    for key in order:
        group = sorted(groups[key], key=_listing_preference)
        winner = group[0]
        variants: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for candidate in group:
            source_variants = candidate.get("listingVariants") or [candidate]
            for variant in source_variants:
                variant_id = str(variant.get("id") or variant.get("link"))
                if variant_id in seen_ids:
                    continue
                seen_ids.add(variant_id)
                variants.append(
                    {
                        "id": variant.get("id"),
                        "link": variant.get("link"),
                        "price": variant.get("price"),
                        "unitPrice": variant.get("unitPrice"),
                        "deliveryLabel": variant.get("deliveryLabel"),
                        "deliveryGroup": variant.get("deliveryGroup"),
                    }
                )
        winner["listingVariants"] = variants
        winner["verifiedModelKey"] = key
        if len(group) > 1:
            winner["dedupeEvidence"] = "verified_brand_title_type_material_length"
        result.append(winner)
    return result


def _decision_summary(item: dict[str, Any], cheapest_price: float | None) -> str:
    positives: list[str] = []
    compromises: list[str] = []
    score = float(item.get("relevanceScore") or 0)
    if score >= 80:
        positives.append("точное совпадение")
    elif score >= 50:
        positives.append("хорошее совпадение")
    reviews = int(item.get("reviews") or 0)
    if reviews >= 50:
        positives.append(f"{reviews} отзывов")
    price = item.get("unitPrice")
    if cheapest_price is not None and price is not None and float(price) <= cheapest_price:
        positives.append("минимальная цена в шорт-листе")
    if item.get("merchantName"):
        positives.append(f"продавец {item['merchantName']}")
    warnings = list(item.get("warnings") or [])
    if warnings:
        compromises.append(warnings[0])
    if item.get("deliveryEvidence") != "seller_offers_api":
        compromises.append("доставка только по поисковой карточке")
    left = ", ".join(positives) or "подходит по основным признакам"
    return left + ("; компромисс: " + compromises[0] if compromises else "")


def _accepted_delivery_groups(delivery_window: str) -> set[str]:
    return {
        "today": {"today"},
        "tomorrow": {"tomorrow"},
        "fast": {"today", "tomorrow"},
        "any": {"today", "tomorrow", "later"},
    }[delivery_window]


def shortlist(args: argparse.Namespace) -> dict[str, Any]:
    search_payload = search(args)
    accepted_groups = _accepted_delivery_groups(args.delivery_window)
    pool = list(search_payload.get("primaryItems", []))
    fetch_count = min(6, args.top + 2)
    candidate_pool = pool[:fetch_count]
    later_search = list(search_payload.get("laterItems", []))[: max(2, args.later_limit)]
    fetched: dict[str, dict[str, Any]] = {}
    requested_qr_mode = args.qr_mode
    args.qr_mode = "none"
    try:
        for index, item in enumerate(candidate_pool + later_search):
            link = item.get("link")
            if not link or link in fetched:
                continue
            fetched[link] = _fetch_product_detail(link, args)
            if index + 1 < len(candidate_pool + later_search) and args.delay:
                time.sleep(args.delay)
    finally:
        args.qr_mode = requested_qr_mode

    enriched_candidates: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    for item in candidate_pool + later_search:
        link = str(item.get("link") or "")
        if link in seen_links:
            continue
        seen_links.add(link)
        enriched_candidates.append(_merge_search_and_detail(item, fetched.get(link, {})))

    enriched_primary: list[dict[str, Any]] = []
    enriched_later: list[dict[str, Any]] = []
    for merged in _dedupe_enriched_items(enriched_candidates):
        if merged.get("deliveryGroup") in accepted_groups:
            enriched_primary.append(merged)
        else:
            enriched_later.append(merged)

    enriched_primary = enriched_primary[: args.top]
    enriched_later = enriched_later[: args.later_limit]
    if requested_qr_mode != "none":
        for item in enriched_primary + enriched_later:
            try:
                if requested_qr_mode == "fallback":
                    _materialize_local_qr(
                        item,
                        args.qr_output_dir,
                        args.timeout,
                        _request_qr_png,
                    )
                else:
                    _materialize_local_qr(item, args.qr_output_dir, args.timeout)
            except RuntimeError as exc:
                item["qrError"] = str(exc)
    prices = [
        float(item["unitPrice"])
        for item in enriched_primary
        if item.get("unitPrice") is not None
    ]
    cheapest = min(prices) if prices else None
    for item in enriched_primary + enriched_later:
        item["decisionSummary"] = _decision_summary(item, cheapest)
    recommendation = enriched_primary[0] if enriched_primary else None
    local_now = datetime.now(ZoneInfo(args.timezone))
    return {
        "source": "Kaspi decision-ready shortlist",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "checkedAtLocal": local_now.isoformat(),
        "cityCode": args.city_code,
        "cityName": args.city_name,
        "zone": args.zone,
        "timezone": args.timezone,
        "requestedDeliveryWindow": args.delivery_window,
        "queries": search_payload.get("queries", []),
        "primaryItems": enriched_primary,
        "laterItems": enriched_later,
        "unverifiedDeliveryItems": search_payload.get("unverifiedDeliveryItems", []),
        "rejectedItems": search_payload.get("rejectedItems", []),
        "recommendation": recommendation,
        "deliveryCaveat": (
            "* Дата и цена продавца взяты из seller-offers API Kaspi для выбранного города/зоны. "
            "Точный адрес и доступность слота всё равно подтверждаются при оформлении."
        ),
        "qrPrivacy": (
            "QR снят из официальной модалки Kaspi и ведёт на официальный l.kaspi.kz app-link; "
            "в него входят только публичная карточка, код города и referrer=desktop_QR."
        ),
    }


def _location_config_path() -> Path:
    override = os.environ.get("KASPI_LOCATION_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "kaspi-skill" / "location.json"


def _load_location_profile() -> dict[str, str]:
    path = _location_config_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    allowed = ("cityCode", "cityName", "zone", "timezone")
    return {key: str(payload[key]) for key in allowed if payload.get(key)}


def _save_location_profile(profile: dict[str, str]) -> Path:
    path = _location_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def location(args: argparse.Namespace) -> dict[str, Any]:
    if args.location_action == "set":
        current = _load_location_profile()
        for key, value in (
            ("cityCode", args.city_code),
            ("cityName", args.city_name),
            ("zone", args.zone),
            ("timezone", args.timezone),
        ):
            if value:
                current[key] = value
        path = _save_location_profile(current)
        return {"location": current, "configPath": str(path), "containsExactAddress": False}
    current = _load_location_profile()
    return {
        "location": current
        or {
            "cityCode": DEFAULT_CITY_CODE,
            "cityName": DEFAULT_CITY_NAME,
            "zone": DEFAULT_ZONE,
            "timezone": DEFAULT_TIMEZONE,
        },
        "configPath": str(_location_config_path()),
        "containsExactAddress": False,
    }


def _resolve_location_args(args: argparse.Namespace) -> None:
    if args.command not in {"search", "details", "shortlist"}:
        return
    profile = _load_location_profile()
    defaults = {
        "city_code": DEFAULT_CITY_CODE,
        "city_name": DEFAULT_CITY_NAME,
        "zone": DEFAULT_ZONE,
        "timezone": DEFAULT_TIMEZONE,
    }
    profile_keys = {
        "city_code": "cityCode",
        "city_name": "cityName",
        "zone": "zone",
        "timezone": "timezone",
    }
    for attribute, fallback in defaults.items():
        if getattr(args, attribute, None) is None:
            setattr(args, attribute, profile.get(profile_keys[attribute], fallback))
    if getattr(args, "no_zone", False):
        args.zone = None
    try:
        ZoneInfo(args.timezone)
    except Exception as exc:
        raise RuntimeError(f"Unknown IANA timezone: {args.timezone}") from exc


def _print_text(payload: dict[str, Any]) -> None:
    if "items" in payload:
        for item in payload["items"]:
            print(
                f"{item['rank']:>2}. {item.get('title') or 'Untitled'} | "
                f"{item.get('price') or item.get('unitPrice') or 'price n/a'} | "
                f"rating={item.get('rating') or 'n/a'} reviews={item.get('reviews') or 'n/a'} | "
                f"delivery={item.get('deliveryLabel') or 'n/a'} | "
                f"{item.get('link') or 'link n/a'}"
            )
        if payload.get("deliveryCaveat"):
            print(f"\n{payload['deliveryCaveat']}")
        return

    if "primaryItems" in payload:
        for item in payload.get("primaryItems", []):
            print(
                f"{item.get('rank', '-'):>2}. {item.get('title') or 'Untitled'} | "
                f"{item.get('price') or item.get('unitPrice') or 'price n/a'} | "
                f"seller={item.get('merchantName') or 'n/a'} | "
                f"delivery={item.get('deliveryLabel') or 'n/a'} | "
                f"relevance={item.get('relevanceScore', 'n/a')} | "
                f"{item.get('link') or 'link n/a'}"
            )
        return

    if "location" in payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    for product in payload.get("products", []):
        print(f"\n### {product.get('title') or 'Untitled'}")
        print(product["url"])
        if product.get("offer"):
            print("OFFER:", json.dumps(product["offer"], ensure_ascii=False))
        if product.get("description"):
            print("DESCRIPTION:", product["description"])
        for spec in product.get("specifications", []):
            print(f"{spec['name']}: {spec['value']}")


def _markdown_escape(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", r"\|").replace("\n", " ")


def _markdown_rows(
    items: list[dict[str, Any]],
    qr_output_dir: str | None = None,
    timeout: float = 20.0,
    qr_fetcher: Callable[[str, float], bytes] | None = None,
) -> list[str]:
    rows = [
        "| Товар | Цена сейчас | Доставка | Ключевые параметры | Рейтинг | Почему / компромисс | QR |",
        "|---|---:|---|---|---:|---|---|",
    ]
    for item in items:
        if not item.get("qrMarkdown"):
            try:
                _materialize_local_qr(item, qr_output_dir, timeout, qr_fetcher)
            except RuntimeError as exc:
                item["qrError"] = str(exc)
        title = _markdown_escape(item.get("title") or "Без названия")
        price = _markdown_escape(item.get("price") or item.get("unitPrice"))
        rating_value = item.get("rating")
        reviews_value = item.get("reviews")
        rating = _markdown_escape(
            "—"
            if rating_value in (None, 0, 0.0)
            else f"{rating_value} · {reviews_value} отзывов" if reviews_value else str(rating_value)
        )
        delivery = _markdown_escape(item.get("deliveryLabel"))
        parameters = item.get("keyParameters") or item.get("category") or []
        if isinstance(parameters, list):
            parameters = "; ".join(str(value) for value in parameters[:4])
        parameters = _markdown_escape(parameters or "—")
        decision = _markdown_escape(item.get("decisionSummary") or "—")
        link = item.get("link")
        product = f"[{title}]({link})" if link else title
        qr = item.get("qrMarkdown") or (f"[открыть]({link})" if link else "—")
        rows.append(
            f"| {product} | {price} | {delivery} | {parameters} | {rating} | {decision} | {qr} |"
        )
    return rows


def _print_markdown(
    payload: dict[str, Any], qr_output_dir: str | None = None, timeout: float = 20.0
) -> None:
    primary = payload.get("primaryItems", [])[:6]
    later = payload.get("laterItems", [])[:2]
    unknown = payload.get("unverifiedDeliveryItems", [])[:2]

    print("## Подходит по доставке\n")
    if primary:
        print("\n".join(_markdown_rows(primary, qr_output_dir, timeout)))
    else:
        print("Подтвержденных вариантов в выбранном окне доставки не найдено.")

    if later:
        print("\n## Можно подождать\n")
        print("\n".join(_markdown_rows(later, qr_output_dir, timeout)))
    if unknown:
        print("\n## Срок надо проверить\n")
        print("\n".join(_markdown_rows(unknown, qr_output_dir, timeout)))
    if payload.get("deliveryCaveat"):
        print(f"\n{payload['deliveryCaveat']}")
    if payload.get("qrPrivacy"):
        print(payload["qrPrivacy"])
    recommendation = payload.get("recommendation")
    if recommendation:
        print(
            f"\n**Мой выбор:** [{_markdown_escape(recommendation.get('title') or 'товар')}]"
            f"({recommendation.get('link')}) — "
            f"{_markdown_escape(recommendation.get('decisionSummary') or 'лучший баланс требований')}."
        )
    city_name = payload.get("cityName") or payload.get("cityCode")
    zone = payload.get("zone")
    if city_name:
        evidence = "seller-offers API" if any(
            item.get("deliveryEvidence") == "seller_offers_api" for item in primary
        ) else "поисковые карточки"
        print(
            f"\n**Доставка:** проверена для {city_name}"
            f"{f', зона `{zone}`' if zone else ''}; источник — {evidence}. Подтвердите адрес и слот в корзине."
        )
    warnings = [
        warning
        for item in primary + later
        for warning in (item.get("warnings") or [])
    ]
    if warnings:
        print(f"\n**Избегать:** {_markdown_escape(warnings[0])}.")
    checked = payload.get("checkedAtLocal") or payload.get("fetchedAt")
    if checked:
        print(f"\n**Проверено:** {_markdown_escape(checked)}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search Kaspi products and inspect shortlisted product pages."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_location_options(target: argparse.ArgumentParser, allow_no_zone: bool = True) -> None:
        target.add_argument("--city-code", help="Kaspi city code; CLI overrides saved profile")
        target.add_argument("--city-name", help="Human-readable city name")
        target.add_argument("--zone", help="Kaspi delivery zone; CLI overrides saved profile")
        target.add_argument("--timezone", help="IANA timezone, for example Asia/Almaty")
        if allow_no_zone:
            target.add_argument("--no-zone", action="store_true", help="Do not filter by a zone")

    def add_network_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--delay", type=float, default=0.8)
        target.add_argument("--timeout", type=float, default=25.0)

    def add_delivery_option(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--delivery-window",
            choices=("today", "tomorrow", "fast", "any"),
            default="fast",
            help=(
                "today=same-day/express, tomorrow=exactly tomorrow, "
                "fast=today or tomorrow (default), any=all verified dates"
            ),
        )

    def add_search_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--query", action="append", required=True, help="Repeat for variants")
        add_location_options(target)
        target.add_argument("--sort", default="relevance")
        target.add_argument("--page", type=int, default=0)
        target.add_argument("--limit", type=int, default=20, choices=range(1, 21), metavar="1-20")
        add_delivery_option(target)
        target.add_argument(
            "--require-term",
            action="append",
            default=[],
            help="Required term; use a|b for alternatives and repeat for AND",
        )
        target.add_argument(
            "--require-material",
            action="append",
            default=[],
            help="Required material; use дерево|бук for alternatives",
        )
        target.add_argument("--exclude-term", action="append", default=[])
        target.add_argument("--min-relevance", type=float, default=20.0)
        add_network_options(target)

    def add_detail_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--offers-limit", type=int, default=20, choices=range(1, 51), metavar="1-50")
        target.add_argument("--no-offers", action="store_true")
        target.add_argument(
            "--qr-mode",
            choices=("official", "fallback", "none"),
            default="official",
            help="official captures Kaspi's modal; fallback is an explicit generic web QR",
        )
        target.add_argument(
            "--qr-output-dir",
            help="Directory for local QR PNGs (default: OS temp cache)",
        )
        target.add_argument("--no-local-qr", action="store_true", help=argparse.SUPPRESS)

    search_parser = subparsers.add_parser("search", help="Search, rank, and deduplicate listings")
    add_search_options(search_parser)
    search_parser.add_argument("--qr-output-dir", help="Directory for Markdown QR PNGs")
    search_parser.add_argument("--format", choices=("json", "text", "markdown"), default="json")
    search_parser.set_defaults(handler=search)

    details_parser = subparsers.add_parser(
        "details", help="Inspect product pages, seller offers, delivery, and official QR"
    )
    details_parser.add_argument("--url", action="append", required=True, help="Repeat for shortlist")
    add_location_options(details_parser)
    add_delivery_option(details_parser)
    add_network_options(details_parser)
    add_detail_options(details_parser)
    details_parser.add_argument("--format", choices=("json", "text"), default="json")
    details_parser.set_defaults(handler=details)

    shortlist_parser = subparsers.add_parser(
        "shortlist", help="One-pass decision-ready search with seller delivery and official QR"
    )
    add_search_options(shortlist_parser)
    add_detail_options(shortlist_parser)
    shortlist_parser.add_argument("--top", type=int, default=4, choices=range(1, 7), metavar="1-6")
    shortlist_parser.add_argument(
        "--later-limit", type=int, default=1, choices=range(0, 3), metavar="0-2"
    )
    shortlist_parser.add_argument(
        "--format", choices=("json", "text", "markdown"), default="markdown"
    )
    shortlist_parser.set_defaults(handler=shortlist)

    location_parser = subparsers.add_parser(
        "location", help="Show or save city/zone defaults without storing an exact address"
    )
    location_parser.add_argument("location_action", choices=("show", "set"), nargs="?", default="show")
    add_location_options(location_parser, allow_no_zone=False)
    location_parser.add_argument("--format", choices=("json", "text"), default="json")
    location_parser.set_defaults(handler=location)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _resolve_location_args(args)
        payload = args.handler(args)
    except (RuntimeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.format == "text":
        _print_text(payload)
    elif args.format == "markdown":
        _print_markdown(payload, args.qr_output_dir, args.timeout)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
