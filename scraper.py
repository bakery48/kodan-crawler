#!/usr/bin/env python3
"""
講談社コミックス新刊スクレイパー（Playwright版）
kc.kodansha.co.jp/new_release から新刊情報を取得して
  data/comics.json … JSON
  data/comics.js  … <script src> でそのまま読める形式（file://対応）
に保存する。

初回セットアップ:
  pip install -r requirements.txt
  playwright install chromium
"""

import json
import re
import time
import hashlib
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

BASE_URL = "https://kc.kodansha.co.jp"
NEW_RELEASE_URL = f"{BASE_URL}/new_release"
JSON_PATH = Path(__file__).parent / "data" / "comics.json"
JS_PATH   = Path(__file__).parent / "data" / "comics.js"


# ─── ユーティリティ ────────────────────────────────────────────

def make_series_id(series_title: str) -> str:
    normalized = re.sub(r"\s+", "", series_title.strip())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]


def parse_volume(title: str) -> tuple[str, str]:
    """タイトルから (シリーズ名, 巻数文字列) を分離する。"""
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
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


# ─── Playwright スクレイピング ──────────────────────────────────

def scrape_with_playwright(months: int = 3, debug: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright

    all_comics: list[dict] = []
    seen_ids: set[str] = set()

    now = datetime.now()
    urls = [NEW_RELEASE_URL]
    for i in range(months):
        m = (now.month - i - 1) % 12 + 1
        y = now.year - ((now.month - i - 1) // 12)
        urls.append(f"{NEW_RELEASE_URL}?year={y}&month={m:02d}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        page = ctx.new_page()

        for url in urls:
            print(f"Fetching: {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception as e:
                print(f"  Navigation error: {e}")
                continue

            # デバッグ: HTMLをファイルに保存
            if debug:
                debug_file = Path(__file__).parent / "data" / "debug_page.html"
                debug_file.write_text(page.content(), encoding="utf-8")
                print(f"  Debug HTML saved to {debug_file}")

            comics = extract_comics_from_page(page)
            print(f"  Found {len(comics)} items")

            for comic in comics:
                if comic["id"] not in seen_ids:
                    seen_ids.add(comic["id"])
                    all_comics.append(comic)

            # ページネーション
            page_num = 2
            while page_num <= 20:
                next_btn = (
                    page.query_selector("a.next")
                    or page.query_selector(".pagination a[rel='next']")
                    or page.query_selector("a:has-text('次へ')")
                    or page.query_selector("a:has-text('>')")
                )
                if not next_btn:
                    break
                href = next_btn.get_attribute("href")
                if not href:
                    break
                next_url = urljoin(BASE_URL, href)
                print(f"  Fetching page {page_num}: {next_url}")
                try:
                    page.goto(next_url, wait_until="networkidle", timeout=30_000)
                except Exception as e:
                    print(f"  Navigation error: {e}")
                    break
                more = extract_comics_from_page(page)
                print(f"    Found {len(more)} items")
                for comic in more:
                    if comic["id"] not in seen_ids:
                        seen_ids.add(comic["id"])
                        all_comics.append(comic)
                page_num += 1

        browser.close()

    return all_comics


def extract_comics_from_page(page) -> list[dict]:
    """ページから個々のコミック情報を抽出する。"""

    # 候補セレクタ群（サイト構造に合わせて優先度順）
    CONTAINER_SELECTORS = [
        "ul.o-section-list__list li",
        ".o-section-list__list li",
        "ul.c-book-list li",
        ".c-book-list__item",
        ".p-book-list__item",
        ".release-list li",
        ".new-release li",
        ".book-list li",
        "li.book",
        "article.book",
        ".product-list li",
        ".item-list li",
        "li[class*='book']",
        "li[class*='product']",
        "li[class*='item']",
    ]

    items = []
    for sel in CONTAINER_SELECTORS:
        els = page.query_selector_all(sel)
        if els:
            items = els
            break

    if not items:
        # JSON-LDからのフォールバック
        return extract_from_json_ld(page)

    results = []
    for el in items:
        comic = parse_element(el, page)
        if comic:
            results.append(comic)
    return results


def parse_element(el, page) -> dict | None:
    """個別のDOM要素からコミック情報をパースする。"""
    try:
        # タイトル
        title_el = (
            el.query_selector(".c-book__title")
            or el.query_selector(".o-book__title")
            or el.query_selector(".p-book__title")
            or el.query_selector("[class*='title']")
            or el.query_selector("h3")
            or el.query_selector("h2")
            or el.query_selector("h4")
            or el.query_selector("strong")
        )
        if not title_el:
            return None
        full_title = title_el.inner_text().strip()
        if not full_title:
            return None
        series_title, volume = parse_volume(full_title)

        # 著者
        author_el = (
            el.query_selector("[class*='author']")
            or el.query_selector("[class*='creator']")
            or el.query_selector(".c-book__author")
            or el.query_selector("p.author")
        )
        author = author_el.inner_text().strip() if author_el else ""

        # 発売日
        date_el = (
            el.query_selector("[class*='date']")
            or el.query_selector("time")
            or el.query_selector("[class*='release']")
        )
        release_date = ""
        if date_el:
            date_text = date_el.get_attribute("datetime") or date_el.inner_text()
            release_date = parse_date(date_text)

        # カバー画像
        img_el = el.query_selector("img")
        cover_url = ""
        if img_el:
            cover_url = (
                img_el.get_attribute("src")
                or img_el.get_attribute("data-src")
                or img_el.get_attribute("data-lazy")
                or ""
            )
            if cover_url and not cover_url.startswith("http"):
                cover_url = urljoin(BASE_URL, cover_url)

        # 詳細URL
        link_el = el.query_selector("a")
        detail_url = ""
        if link_el:
            href = link_el.get_attribute("href") or ""
            if href:
                detail_url = urljoin(BASE_URL, href)

        # ラベル
        label_el = (
            el.query_selector("[class*='label']")
            or el.query_selector("[class*='imprint']")
            or el.query_selector("[class*='magazine']")
        )
        label = label_el.inner_text().strip() if label_el else ""

        # ISBN
        isbn = el.get_attribute("data-isbn") or el.get_attribute("data-code") or ""

        sid = make_series_id(series_title)
        return {
            "id": sid + (f"_{volume}" if volume else ""),
            "series_id": sid,
            "series_title": series_title,
            "full_title": full_title,
            "volume": volume,
            "author": author,
            "release_date": release_date,
            "cover_url": cover_url,
            "detail_url": detail_url,
            "label": label,
            "isbn": isbn,
        }
    except Exception as e:
        return None


def extract_from_json_ld(page) -> list[dict]:
    """JSON-LDスキーマからフォールバック取得する。"""
    results = []
    try:
        scripts = page.query_selector_all('script[type="application/ld+json"]')
        for s in scripts:
            raw = s.inner_text()
            data = json.loads(raw)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and "@graph" in data:
                items = data["@graph"]
            else:
                items = [data]
            for item in items:
                if item.get("@type") not in ("Book", "Product", "CreativeWork"):
                    continue
                name = item.get("name", "")
                if not name:
                    continue
                series_title, volume = parse_volume(name)
                sid = make_series_id(series_title)
                release_date = parse_date(item.get("datePublished", ""))
                results.append({
                    "id": sid + (f"_{volume}" if volume else ""),
                    "series_id": sid,
                    "series_title": series_title,
                    "full_title": name,
                    "volume": volume,
                    "author": item.get("author", {}).get("name", "") if isinstance(item.get("author"), dict) else "",
                    "release_date": release_date,
                    "cover_url": item.get("image", ""),
                    "detail_url": item.get("url", ""),
                    "label": "",
                    "isbn": item.get("isbn", ""),
                })
    except Exception:
        pass
    return results


# ─── 保存 ──────────────────────────────────────────────────────

def load_existing(path: Path) -> list[dict]:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("comics", [])
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
        "count": len(sorted_comics),
        "comics": sorted_comics,
    }

    # JSON
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(sorted_comics)} comics to {JSON_PATH}")

    # JS (file:// でも読めるよう var に代入)
    js_content = "var COMICS_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write(js_content)
    print(f"Saved JS data to {JS_PATH}")


# ─── メイン ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="講談社コミックス新刊スクレイパー")
    parser.add_argument("--months", type=int, default=3, help="取得する月数 (default: 3)")
    parser.add_argument("--debug", action="store_true", help="HTMLをファイルに保存して確認")
    parser.add_argument("--no-merge", action="store_true", help="既存データを無視して上書き")
    args = parser.parse_args()

    print("=== 講談社コミックス新刊スクレイパー ===")
    print(f"Target: {NEW_RELEASE_URL}")
    print()

    new_comics = scrape_with_playwright(months=args.months, debug=args.debug)
    print(f"\nFetched {len(new_comics)} new comics")

    existing = [] if args.no_merge else load_existing(JSON_PATH)
    merged = merge_comics(existing, new_comics)

    save(merged)
    print("Done!")


if __name__ == "__main__":
    main()
