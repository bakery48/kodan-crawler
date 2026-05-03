#!/usr/bin/env python3
"""
講談社コミックス新刊スクレイパー（Playwright + OpenBD版）

仕組み:
  1. Playwright でページを開きながら XHR/fetch レスポンスを傍受
  2. サイトが呼ぶ JSON API を自動検出してコミックデータを取得
  3. ISBN があれば OpenBD API で表紙画像 URL を補完
  4. DOM パースにもフォールバック

初回セットアップ:
  pip install -r requirements.txt
  playwright install chromium

実行例:
  python scraper.py               # 直近3ヶ月
  python scraper.py --months 6    # 直近6ヶ月
  python scraper.py --debug       # APIレスポンスを data/debug_api.json に保存
  python scraper.py --no-merge    # 既存データを無視して上書き
"""

import json
import re
import time
import hashlib
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests as req_lib

BASE_URL        = "https://www.kodansha.co.jp"
NEW_RELEASE_URL = f"{BASE_URL}/comic/new-releases"
JSON_PATH = Path(__file__).parent / "data" / "comics.json"
JS_PATH   = Path(__file__).parent / "data" / "comics.js"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ─── ユーティリティ ────────────────────────────────────────────

def make_series_id(title: str) -> str:
    normalized = re.sub(r"\s+", "", title.strip())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def parse_volume(title: str) -> tuple[str, str]:
    """タイトルから (シリーズ名, 巻数) を分離する。"""
    patterns = [
        r"^(.+?)[\s　]*[（(](\d+)[）)]\s*$",
        r"^(.+?)[\s　]+第?(\d+)巻?\s*$",
        r"^(.+?)[\s　]+[Vv][Oo][Ll]\.?\s*(\d+)\s*$",
    ]
    for pat in patterns:
        m = re.match(pat, title.strip())
        if m:
            return m.group(1).strip(), m.group(2)
    return title.strip(), ""


def parse_date(text: str) -> str:
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", str(text))
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def normalize_isbn(s: str) -> str:
    return re.sub(r"[^0-9X]", "", str(s).upper())


# ─── OpenBD 表紙画像補完 ────────────────────────────────────────

def fetch_openbd_covers(isbns: list[str]) -> dict[str, str]:
    """ISBN のリストを受け取り {isbn: cover_url} を返す。"""
    if not isbns:
        return {}
    covers: dict[str, str] = {}
    batch_size = 1000
    for i in range(0, len(isbns), batch_size):
        batch = isbns[i : i + batch_size]
        try:
            resp = req_lib.get(
                "https://api.openbd.jp/v1/get",
                params={"isbn": ",".join(batch)},
                timeout=30,
                headers={"User-Agent": BROWSER_UA},
            )
            resp.raise_for_status()
            for item in resp.json():
                if not item:
                    continue
                summary = item.get("summary", {})
                isbn    = normalize_isbn(summary.get("isbn", ""))
                cover   = summary.get("cover", "")
                if isbn and cover:
                    covers[isbn] = cover
        except Exception as e:
            print(f"  OpenBD error: {e}")
        time.sleep(0.5)
    return covers


def enrich_covers(comics: list[dict]) -> list[dict]:
    """カバーURLが空のコミックを OpenBD で補完する。"""
    need = [c["isbn"] for c in comics if c.get("isbn") and not c.get("cover_url")]
    if not need:
        return comics
    print(f"Fetching covers from OpenBD for {len(need)} books...")
    covers = fetch_openbd_covers(need)
    for c in comics:
        if c.get("isbn") and not c.get("cover_url"):
            c["cover_url"] = covers.get(normalize_isbn(c["isbn"]), "")
    print(f"  Got {len(covers)} cover images")
    return comics


# ─── API レスポンス傍受 ────────────────────────────────────────

def is_comic_entry(obj) -> bool:
    """dict がコミック1冊のデータらしいか判定する。"""
    if not isinstance(obj, dict):
        return False
    keys = {k.lower() for k in obj.keys()}
    has_title  = bool(keys & {"title", "タイトル", "booktitle", "name", "bookname"})
    has_date   = bool(keys & {"date", "releasedate", "publishdate", "ondate", "saledate", "発売日"})
    has_isbn   = bool(keys & {"isbn", "isbn13", "jan", "code", "productcode"})
    return has_title or has_isbn or has_date


def walk_json(obj, depth=0, max_depth=6) -> list[dict]:
    """JSON 構造を再帰的に走査してコミックらしい dict のリストを返す。"""
    if depth > max_depth:
        return []
    if isinstance(obj, list):
        # リストの中身全部がコミックらしければそのまま返す
        hits = [x for x in obj if is_comic_entry(x)]
        if len(hits) >= 3:
            return hits
        # 子要素を再帰
        results = []
        for item in obj:
            results.extend(walk_json(item, depth + 1, max_depth))
        return results
    if isinstance(obj, dict):
        if is_comic_entry(obj):
            return [obj]
        results = []
        for v in obj.values():
            results.extend(walk_json(v, depth + 1, max_depth))
        return results
    return []


def parse_api_entry(entry: dict) -> dict | None:
    """API から取得した1冊分のデータを正規化する。"""
    def get(*keys):
        for k in keys:
            for kk in (k, k.lower(), k.upper()):
                if kk in entry:
                    v = entry[kk]
                    if v:
                        return str(v).strip()
        return ""

    # タイトル
    full_title = get("title", "booktitle", "bookTitle", "name", "タイトル", "bookname")
    if not full_title:
        return None
    series_title, volume = parse_volume(full_title)

    # 著者
    author = get("author", "creator", "著者", "authorName", "author_name")

    # 発売日
    release_date = parse_date(
        get("releaseDate", "release_date", "saleDate", "sale_date",
            "publishDate", "onDate", "date", "発売日")
    )

    # カバー画像
    cover_url = get("coverImageUrl", "cover_image", "image", "thumbnail",
                    "coverUrl", "imageUrl", "img", "表紙")
    if cover_url and not cover_url.startswith("http"):
        cover_url = urljoin(BASE_URL, cover_url)

    # 詳細URL
    detail_url = get("url", "link", "detailUrl", "productUrl")
    if detail_url and not detail_url.startswith("http"):
        detail_url = urljoin(BASE_URL, detail_url)

    # ISBN
    isbn = normalize_isbn(get("isbn", "isbn13", "jan", "productCode", "code"))

    # ラベル
    label = get("label", "labelName", "magazine", "imprint", "series")

    sid = make_series_id(series_title)
    return {
        "id":           sid + (f"_{volume}" if volume else ""),
        "series_id":    sid,
        "series_title": series_title,
        "full_title":   full_title,
        "volume":       volume,
        "author":       author,
        "release_date": release_date,
        "cover_url":    cover_url,
        "detail_url":   detail_url,
        "label":        label,
        "isbn":         isbn,
    }


# ─── Playwright スクレイピング ──────────────────────────────────

def scrape_page(page, url: str, debug_responses: list) -> list[dict]:
    """1ページ分のコミックを取得する（API傍受 → DOMフォールバック）。"""
    captured: list[dict] = []

    def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct or response.status != 200:
            return
        # 静的アセット・アナリティクス系は除外
        skip_patterns = ("analytics", "gtm", "segment", "sentry",
                         "hotjar", "clarity", "cdn", "fonts")
        if any(p in response.url.lower() for p in skip_patterns):
            return
        try:
            data = response.json()
            debug_responses.append({"url": response.url, "data": data})
            entries = walk_json(data)
            if entries:
                print(f"    [API] {response.url}  → {len(entries)} entries")
                captured.extend(entries)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception as e:
        print(f"  Navigation error: {e}")
        return []

    # レンダリング後のHTMLを保存（デバッグ用・毎回上書き）
    html_path = Path(__file__).parent / "data" / "debug_page.html"
    html_path.write_text(page.content(), encoding="utf-8")
    print(f"  Rendered HTML saved to {html_path}")

    # API 傍受でデータが取れた場合
    if captured:
        results = []
        for entry in captured:
            comic = parse_api_entry(entry)
            if comic:
                results.append(comic)
        return results

    # フォールバック: DOM パース
    print("  No API data captured, falling back to DOM parse...")
    return extract_from_dom(page)


def extract_from_dom(page) -> list[dict]:
    """DOM からコミック情報を抽出する。"""
    SELECTORS = [
        "ul.o-section-list__list li",
        ".o-section-list__list li",
        "ul.c-book-list li",
        ".c-book-list__item",
        ".p-book-list__item",
        ".release-list li",
        ".book-list li",
        "li.book",
        "article.book",
        "li[class*='book']",
        "li[class*='product']",
        "li[class*='item']",
        ".product-list li",
    ]
    items = []
    for sel in SELECTORS:
        els = page.query_selector_all(sel)
        if els:
            items = els
            break

    if not items:
        # JSON-LD スキーマ
        return extract_from_json_ld(page)

    results = []
    for el in items:
        comic = parse_dom_element(el)
        if comic:
            results.append(comic)
    return results


def parse_dom_element(el) -> dict | None:
    def text(selectors):
        for sel in selectors:
            try:
                node = el.query_selector(sel)
                if node:
                    t = node.inner_text().strip()
                    if t:
                        return t
            except Exception:
                pass
        return ""

    def attr(selectors, attribute):
        for sel in selectors:
            try:
                node = el.query_selector(sel)
                if node:
                    v = node.get_attribute(attribute)
                    if v:
                        return v.strip()
            except Exception:
                pass
        return ""

    full_title = text(["[class*='title']", "h3", "h2", "h4", "strong"])
    if not full_title:
        return None
    series_title, volume = parse_volume(full_title)

    author  = text(["[class*='author']", "[class*='creator']", "p.author"])
    date_raw = text(["[class*='date']", "[class*='release']", "time"])
    release_date = parse_date(date_raw)

    img = el.query_selector("img")
    cover_url = ""
    if img:
        cover_url = (
            img.get_attribute("src")
            or img.get_attribute("data-src")
            or img.get_attribute("data-lazy")
            or ""
        )
        if cover_url and not cover_url.startswith("http"):
            cover_url = urljoin(BASE_URL, cover_url)

    link = el.query_selector("a")
    detail_url = ""
    if link:
        href = link.get_attribute("href") or ""
        if href:
            detail_url = urljoin(BASE_URL, href)

    isbn = (el.get_attribute("data-isbn") or el.get_attribute("data-code") or "")
    isbn = normalize_isbn(isbn)
    label = text(["[class*='label']", "[class*='imprint']", "[class*='magazine']"])

    sid = make_series_id(series_title)
    return {
        "id":           sid + (f"_{volume}" if volume else ""),
        "series_id":    sid,
        "series_title": series_title,
        "full_title":   full_title,
        "volume":       volume,
        "author":       author,
        "release_date": release_date,
        "cover_url":    cover_url,
        "detail_url":   detail_url,
        "label":        label,
        "isbn":         isbn,
    }


def extract_from_json_ld(page) -> list[dict]:
    results = []
    try:
        for s in page.query_selector_all('script[type="application/ld+json"]'):
            data = json.loads(s.inner_text())
            items = data.get("@graph", [data]) if isinstance(data, dict) else data
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in ("Book", "Product", "CreativeWork"):
                    continue
                name = item.get("name", "")
                if not name:
                    continue
                series_title, volume = parse_volume(name)
                sid = make_series_id(series_title)
                author_obj = item.get("author", {})
                author = author_obj.get("name", "") if isinstance(author_obj, dict) else ""
                results.append({
                    "id":           sid + (f"_{volume}" if volume else ""),
                    "series_id":    sid,
                    "series_title": series_title,
                    "full_title":   name,
                    "volume":       volume,
                    "author":       author,
                    "release_date": parse_date(item.get("datePublished", "")),
                    "cover_url":    item.get("image", ""),
                    "detail_url":   item.get("url", ""),
                    "label":        "",
                    "isbn":         normalize_isbn(item.get("isbn", "")),
                })
    except Exception:
        pass
    return results


# ─── メインスクレイプループ ────────────────────────────────────

def scrape_all(months: int, debug: bool) -> list[dict]:
    from playwright.sync_api import sync_playwright

    now   = datetime.now()
    # メインの新刊一覧ページ
    urls  = [NEW_RELEASE_URL]
    # 月別ページ（新サイトのURLパターンが判明次第ここに追加）
    # 例: urls.append(f"{NEW_RELEASE_URL}?year={y}&month={m:02d}")
    for i in range(months):
        m = (now.month - i - 1) % 12 + 1
        y = now.year - ((now.month - i - 1) // 12)
        urls.append(f"{NEW_RELEASE_URL}?year={y}&month={m:02d}")

    all_comics: list[dict] = []
    seen_ids: set[str]     = set()
    debug_responses: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=BROWSER_UA, locale="ja-JP")
        page = ctx.new_page()

        for url in urls:
            print(f"Fetching: {url}")
            comics = scrape_page(page, url, debug_responses)
            print(f"  Found {len(comics)} items")

            for c in comics:
                if c["id"] not in seen_ids:
                    seen_ids.add(c["id"])
                    all_comics.append(c)

            # ページネーション
            page_num = 2
            while page_num <= 30:
                next_btn = (
                    page.query_selector("a.next")
                    or page.query_selector(".pagination a[rel='next']")
                    or page.query_selector("a:has-text('次へ')")
                    or page.query_selector("a:has-text('次のページ')")
                )
                if not next_btn:
                    break
                href = next_btn.get_attribute("href") or ""
                if not href:
                    break
                next_url = urljoin(BASE_URL, href)
                print(f"  Page {page_num}: {next_url}")
                comics = scrape_page(page, next_url, debug_responses)
                print(f"    Found {len(comics)} items")
                for c in comics:
                    if c["id"] not in seen_ids:
                        seen_ids.add(c["id"])
                        all_comics.append(c)
                page_num += 1

        browser.close()

    if debug and debug_responses:
        debug_path = Path(__file__).parent / "data" / "debug_api.json"
        debug_path.write_text(
            json.dumps(debug_responses, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Debug API responses saved to {debug_path}  ({len(debug_responses)} responses)")
    elif debug:
        print("  (No JSON API responses captured)")

    return all_comics


# ─── 保存 ──────────────────────────────────────────────────────

def load_existing() -> list[dict]:
    if JSON_PATH.exists():
        try:
            return json.loads(JSON_PATH.read_text(encoding="utf-8")).get("comics", [])
        except Exception:
            pass
    return []


def merge_comics(existing: list[dict], new: list[dict]) -> list[dict]:
    merged = {c["id"]: c for c in existing}
    for c in new:
        merged[c["id"]] = c
    return list(merged.values())


def save(comics: list[dict]) -> None:
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    sorted_comics = sorted(
        comics,
        key=lambda c: c.get("release_date") or "0000-00-00",
        reverse=True,
    )
    payload = {
        "generated_at": datetime.now().isoformat(),
        "count":        len(sorted_comics),
        "comics":       sorted_comics,
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(sorted_comics)} comics to {JSON_PATH}")

    js = "var COMICS_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    JS_PATH.write_text(js, encoding="utf-8")
    print(f"Saved JS data to {JS_PATH}")


# ─── メイン ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="講談社コミックス新刊スクレイパー")
    parser.add_argument("--months",   type=int, default=3,    help="取得する月数 (default: 3)")
    parser.add_argument("--debug",    action="store_true",    help="APIレスポンスを data/debug_api.json に保存")
    parser.add_argument("--no-merge", action="store_true",    help="既存データを無視して上書き")
    parser.add_argument("--no-cover", action="store_true",    help="OpenBD での表紙取得をスキップ")
    args = parser.parse_args()

    print("=== 講談社コミックス新刊スクレイパー ===")
    print(f"Target : {NEW_RELEASE_URL}")
    print(f"Months : {args.months}")
    print()

    new_comics = scrape_all(months=args.months, debug=args.debug)
    print(f"\nScraped {len(new_comics)} comics from site")

    if not args.no_cover:
        new_comics = enrich_covers(new_comics)

    existing = [] if args.no_merge else load_existing()
    merged   = merge_comics(existing, new_comics)

    save(merged)
    print("Done!")


if __name__ == "__main__":
    main()
