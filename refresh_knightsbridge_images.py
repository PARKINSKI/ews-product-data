#!/usr/bin/env python3
"""
Rebuild clear Knightsbridge image URLs in ews-products.json.

The script deliberately does NOT upscale the existing catalogue crops. It finds a
live product page for each exact Knightsbridge SKU, extracts candidate product
images, verifies their actual pixel dimensions, and only replaces the JSON URL
when a sufficiently large image is found.

Sources, in order:
  1. ML Accessories / Knightsbridge official site (when discoverable)
  2. Westbase Direct (Shopify product pages; predictable /products/<sku>)
  3. Lamplec product pages (predictable ProductDetails/?code=<sku>)

No repository images are modified. Only ews-products.json and a report are
written. Wildcard/group SKUs are skipped intentionally.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote, quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 14
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MIN_SQUARE_SIDE = 500
MIN_LONG_SIDE = 900
MIN_PIXELS = 250_000

_tls = threading.local()


def session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "en-GB,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            }
        )
        _tls.session = s
    return s


def clean_text(value: object) -> str:
    return str(value or "").strip()


def is_exact_sku(code: str) -> bool:
    """Reject catalogue/group placeholders, but allow normal SKU punctuation."""
    code = clean_text(code)
    if not code or "*" in code or " " in code:
        return False
    if re.search(r"x{2,}$", code, flags=re.I):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+\-]*", code))


def sku_in_html(html: str, sku: str) -> bool:
    # Exact-ish token match, avoiding e.g. RE6CTF matching RE6CTF10.
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(sku)}(?![A-Za-z0-9])", re.I)
    return bool(pat.search(html))


def get_html(url: str) -> tuple[Optional[str], Optional[str], int]:
    try:
        r = session().get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return None, r.url, r.status_code
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype and not r.text.lstrip().startswith("<"):
            return None, r.url, r.status_code
        return r.text, r.url, r.status_code
    except requests.RequestException:
        return None, None, 0


def normalize_image_url(url: str, base: str) -> Optional[str]:
    if not url:
        return None
    url = unescape(url).strip().strip('"\'')
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    else:
        url = urljoin(base, url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None

    # Shopify often serves a resized copy via ?width=. Removing only the width
    # query requests the original asset while preserving signed/other params is
    # risky, so do this only for known Shopify CDN hosts.
    if "cdn.shopify.com" in parsed.netloc or "shopifycdn.net" in parsed.netloc:
        # Shopify original is generally the same path without query parameters.
        url = parsed._replace(query="", fragment="").geturl()
    return url


def srcset_urls(srcset: str, base: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        u = normalize_image_url(bits[0], base)
        if not u:
            continue
        score = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)w", bits[1])
            if m:
                score = int(m.group(1))
        out.append((score, u))
    return out


def walk_json_images(obj: object, sku: str) -> list[str]:
    """Extract image URLs from JSON-LD objects relevant to the requested SKU."""
    found: list[str] = []

    def rec(x: object, relevant: bool = False) -> None:
        if isinstance(x, dict):
            local_relevant = relevant
            sku_val = clean_text(x.get("sku") or x.get("mpn") or x.get("productID"))
            if sku_val and sku_val.upper() == sku.upper():
                local_relevant = True
            typ = x.get("@type")
            if isinstance(typ, str) and typ.lower() == "product" and sku.upper() in json.dumps(x).upper():
                local_relevant = True
            if local_relevant and "image" in x:
                im = x["image"]
                if isinstance(im, str):
                    found.append(im)
                elif isinstance(im, list):
                    for y in im:
                        if isinstance(y, str):
                            found.append(y)
                        elif isinstance(y, dict):
                            for k in ("url", "contentUrl"):
                                if isinstance(y.get(k), str):
                                    found.append(y[k])
                elif isinstance(im, dict):
                    for k in ("url", "contentUrl"):
                        if isinstance(im.get(k), str):
                            found.append(im[k])
            for v in x.values():
                rec(v, local_relevant)
        elif isinstance(x, list):
            for y in x:
                rec(y, relevant)

    rec(obj)
    return found


def extract_candidates(html: str, page_url: str, sku: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    scored: dict[str, int] = {}

    def add(raw: Optional[str], score: int) -> None:
        u = normalize_image_url(raw or "", page_url)
        if not u:
            return
        low = u.lower()
        # Exclude obvious non-product assets.
        if any(t in low for t in ("logo", "icon", "badge", "payment", "sprite", "avatar")):
            return
        scored[u] = max(scored.get(u, -999), score)

    # OpenGraph/Twitter primary images are usually the safest product photo.
    for selector, score in (
        ('meta[property="og:image:secure_url"]', 120),
        ('meta[property="og:image"]', 115),
        ('meta[name="twitter:image"]', 110),
        ('meta[property="twitter:image"]', 110),
    ):
        for tag in soup.select(selector):
            add(tag.get("content"), score)

    # Structured product data.
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            obj = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        for u in walk_json_images(obj, sku):
            add(u, 125)

    # Shopify product JSON embedded in the page often contains featured_image.
    for m in re.finditer(r'"(?:featured_image|featuredImage|image)"\s*:\s*"([^"\\]+(?:\\.[^"\\]*)*)"', html):
        raw = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
        add(raw, 100)

    # Product-gallery images. Prefer exact SKU/"main"/"product" context and
    # largest srcset candidate.
    for img in soup.find_all("img"):
        alt = clean_text(img.get("alt"))
        classes = " ".join(img.get("class") or [])
        parent_classes = " ".join((img.parent.get("class") or []) if img.parent else [])
        context = f"{alt} {classes} {parent_classes} {img.get('id','')}".lower()
        base_score = 35
        if sku.lower() in context:
            base_score += 55
        if any(t in context for t in ("product", "main", "featured", "gallery", "zoom")):
            base_score += 30
        ss = srcset_urls(img.get("srcset") or img.get("data-srcset") or "", page_url)
        for width, u in ss:
            add(u, base_score + min(width // 100, 20))
        for attr in ("data-zoom", "data-zoom-image", "data-src", "data-original", "src"):
            add(img.get(attr), base_score)

    return [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1], reverse=True)]


def image_dimensions(url: str, referer: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    try:
        headers = {"Referer": referer, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}
        r = session().get(url, headers=headers, timeout=DEFAULT_TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None, None, None
        ctype = (r.headers.get("content-type") or "").lower()
        if "svg" in ctype or url.lower().split("?")[0].endswith(".svg"):
            return None, None, None
        buf = io.BytesIO()
        total = 0
        for chunk in r.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                return None, None, None
            buf.write(chunk)
        buf.seek(0)
        with Image.open(buf) as im:
            w, h = im.size
            return int(w), int(h), r.url
    except Exception:
        return None, None, None


def is_clear(w: int, h: int) -> bool:
    if w <= 0 or h <= 0 or w * h < MIN_PIXELS:
        return False
    short, long = sorted((w, h))
    return short >= MIN_SQUARE_SIDE or long >= MIN_LONG_SIDE


def westbase_page(sku: str) -> str:
    # Shopify handle observed on Westbase uses lower-case SKU for Knightsbridge.
    handle = sku.lower().replace("/", "-").replace("+", "-")
    handle = re.sub(r"[^a-z0-9._-]+", "-", handle).strip("-")
    return f"https://westbasedirect.com/products/{quote(handle)}"


def lamplec_page(sku: str) -> str:
    return f"https://www.lamplec.co.uk/ProductDetails/?code={quote_plus(sku)}"


def discover_ml_page(sku: str) -> Optional[str]:
    # ML Accessories has changed search implementations over time. Try a small
    # set of harmless GET search variants and accept only /product/ links where
    # the returned page itself contains the exact requested SKU.
    queries = [
        f"https://www.mlaccessories.co.uk/search?q={quote_plus(sku)}",
        f"https://www.mlaccessories.co.uk/search?search={quote_plus(sku)}",
        f"https://www.mlaccessories.co.uk/search?query={quote_plus(sku)}",
        f"https://www.mlaccessories.co.uk/search?keywords={quote_plus(sku)}",
    ]
    seen: set[str] = set()
    for q in queries:
        html, final, status = get_html(q)
        if not html or status != 200:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(final or q, a["href"])
            if "/product/" not in href or href in seen:
                continue
            seen.add(href)
            label = f"{a.get_text(' ', strip=True)} {a.get('title','')}"
            if sku.lower() not in label.lower() and sku.lower() not in html.lower():
                continue
            ph, pf, ps = get_html(href)
            if ph and ps == 200 and sku_in_html(ph, sku):
                return pf or href
    return None


@dataclass
class Result:
    code: str
    status: str
    image: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    source: Optional[str] = None
    page: Optional[str] = None
    reason: Optional[str] = None


def try_page(sku: str, source: str, page_url: str) -> Optional[Result]:
    html, final_url, status = get_html(page_url)
    if not html or status != 200:
        return None
    final_url = final_url or page_url
    if not sku_in_html(html, sku):
        return None
    candidates = extract_candidates(html, final_url, sku)
    for u in candidates[:18]:
        w, h, resolved = image_dimensions(u, final_url)
        if w is None or h is None or not resolved:
            continue
        if is_clear(w, h):
            return Result(
                code=sku,
                status="updated",
                image=resolved,
                width=w,
                height=h,
                source=source,
                page=final_url,
            )
    return Result(code=sku, status="no-clear-image", source=source, page=final_url, reason="page matched SKU but no candidate met resolution threshold")


def resolve_sku(sku: str) -> Result:
    # Prefer official page if it can be discovered cheaply from ML's own search.
    # In practice Westbase/Lamplec provide predictable exact-SKU routes and are
    # tried first to avoid four search requests for every single SKU.
    wb = try_page(sku, "westbase", westbase_page(sku))
    if wb and wb.status == "updated":
        return wb

    lp = try_page(sku, "lamplec", lamplec_page(sku))
    if lp and lp.status == "updated":
        return lp

    ml = discover_ml_page(sku)
    if ml:
        off = try_page(sku, "mlaccessories", ml)
        if off and off.status == "updated":
            return off

    reasons = []
    for r in (wb, lp):
        if r and r.reason:
            reasons.append(f"{r.source}: {r.reason}")
    return Result(code=sku, status="unmatched", reason="; ".join(reasons) or "no exact product page with a clear image found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="ews-products.json")
    ap.add_argument("--output", default="ews-products.json")
    ap.add_argument("--report", default="knightsbridge-image-refresh-report.json")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="For testing: resolve at most N exact Knightsbridge SKUs")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 2

    products = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(products, list):
        print("ERROR: product JSON must be an array", file=sys.stderr)
        return 2

    knight_indices: dict[str, list[int]] = {}
    wildcard_codes: list[str] = []
    for i, p in enumerate(products):
        if clean_text(p.get("brand")) != "Knightsbridge":
            continue
        code = clean_text(p.get("code"))
        if not is_exact_sku(code):
            wildcard_codes.append(code)
            continue
        knight_indices.setdefault(code, []).append(i)

    codes = list(knight_indices)
    if args.limit:
        codes = codes[: args.limit]

    print(f"Products: {len(products)}")
    print(f"Knightsbridge rows: {sum(len(v) for v in knight_indices.values()) + len(wildcard_codes)}")
    print(f"Exact Knightsbridge SKUs to resolve: {len(codes)}")
    print(f"Wildcard/group rows intentionally skipped: {len(wildcard_codes)}")

    results: dict[str, Result] = {}
    started = time.time()
    workers = max(1, min(args.workers, 10))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(resolve_sku, code): code for code in codes}
        done = 0
        for fut in cf.as_completed(futures):
            code = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = Result(code=code, status="error", reason=str(e))
            results[code] = res
            done += 1
            if res.status == "updated":
                print(f"[{done}/{len(codes)}] OK {code}: {res.width}x{res.height} ({res.source})")
            elif done % 50 == 0:
                print(f"[{done}/{len(codes)}] processed...")

    updated_rows = 0
    source_counts: dict[str, int] = {}
    for code, res in results.items():
        if res.status != "updated" or not res.image:
            continue
        for idx in knight_indices.get(code, []):
            p = products[idx]
            p["image"] = res.image
            # Remove stale local catalogue-crop pointer so consumers don't pick
            # the blurry file in preference to the new image URL.
            p.pop("imageFile", None)
            p["imageMapping"] = "knightsbridge-clear-verified-product-photo"
            updated_rows += 1
        source_counts[res.source or "unknown"] = source_counts.get(res.source or "unknown", 0) + 1

    out = Path(args.output)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Validate the exact file we're about to publish.
    json.loads(tmp.read_text(encoding="utf-8"))
    tmp.replace(out)

    updated_skus = sum(1 for r in results.values() if r.status == "updated")
    report = {
        "input": str(src),
        "output": str(out),
        "totalProducts": len(products),
        "knightsbridgeRows": sum(1 for p in products if clean_text(p.get("brand")) == "Knightsbridge"),
        "exactSkusAttempted": len(codes),
        "updatedSkus": updated_skus,
        "updatedRows": updated_rows,
        "wildcardGroupRowsSkipped": len(wildcard_codes),
        "sourceCounts": source_counts,
        "minimumImageRule": {
            "minimumPixels": MIN_PIXELS,
            "minimumShortSideForStandardImages": MIN_SQUARE_SIDE,
            "minimumLongSideForNarrowImages": MIN_LONG_SIDE,
        },
        "elapsedSeconds": round(time.time() - started, 1),
        "wildcardCodes": wildcard_codes,
        "results": [asdict(results[c]) for c in codes],
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\nDONE")
    print(f"Updated exact SKUs: {updated_skus}/{len(codes)}")
    print(f"Updated product rows: {updated_rows}")
    print(f"Source counts: {source_counts}")
    print(f"JSON: {out}")
    print(f"Report: {args.report}")

    # Do not fail the build merely because some old/discontinued SKUs have no
    # current product photo. The report makes omissions explicit.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
