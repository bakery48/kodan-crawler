#!/usr/bin/env python3
"""
講談社コミックス新刊スクレイパー
kc.kodansha.co.jp/new_release から新刊情報を取得して data/comics.json に保存する
"""

import json
import re
import time
import hashlib
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://kc.kodansha.co.jp"
NEW_RELEASE_URL = f"{BASE_URL}/new_release"
OUTPUT_PATH = Path(__file__).parent / "data" / "comics.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}


def make_series_id(series_title: str) -> str:
    """シリーズタイトルからIDを生成する"""
    normalized = re.sub(r"\s+", "", series_title.strip())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]


def parse_volume(title: str) -> tuple[str, str]:
    """タイトルからシリーズ名と巻数を分離する
    例: "進撃の巨人（34）" -> ("進撃の巨人", "34")
    """
    # パターン: タイトル（巻数）or タイトル 巻数巻 or タイトル(n)
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


def fetch_page(session: requests.Session, url: str, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            return BeautifulSoup(resp.text, "html.parser")
        except requests.HTTPError as e:
            print(f"  HTTP {e.response.status_code}: {url}")
            if e.response.status_code in (403, 404):
                return None
            time.sleep(2 ** attempt)
        except requests.RequestException as e:
            print(f"  Request error ({attempt + 1}/{retries}): {e}")
            time.sleep(2 ** attempt)
    return None


def parse_comic_item(item) -> dict | None:
    """個別のコミックHTML要素をパースする"""
    try:
        # タイトル
        title_el = (
            item.select_one(".item-title")
            or item.select_one(".product-name")
            or item.select_one("h3")
            or item.select_one("h2")
            or item.select_one(".title")
        )
        if not title_el:
            return None
        full_title = title_el.get_text(strip=True)
        series_title, volume = parse_volume(full_title)

        # 著者
        author_el = (
            item.select_one(".item-author")
            or item.select_one(".author")
            or item.select_one(".creator")
        )
        author = author_el.get_text(strip=True) if author_el else ""

        # 発売日
        date_el = (
            item.select_one(".item-date")
            or item.select_one(".release-date")
            or item.select_one("time")
            or item.select_one(".date")
        )
        release_date = ""
        if date_el:
            date_text = date_el.get("datetime") or date_el.get_text(strip=True)
            m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", date_text)
            if m:
                release_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # カバー画像
        img_el = item.select_one("img")
        cover_url = ""
        if img_el:
            cover_url = img_el.get("src") or img_el.get("data-src") or ""
            if cover_url and not cover_url.startswith("http"):
                cover_url = urljoin(BASE_URL, cover_url)

        # 詳細URL
        link_el = item.select_one("a")
        detail_url = ""
        if link_el:
            href = link_el.get("href", "")
            if href:
                detail_url = urljoin(BASE_URL, href)

        # ISBN / product code
        isbn = item.get("data-isbn") or item.get("data-code") or ""

        # ラベル (KC, マガジン KC, etc.)
        label_el = item.select_one(".label") or item.select_one(".imprint")
        label = label_el.get_text(strip=True) if label_el else ""

        return {
            "id": make_series_id(series_title) + (f"_{volume}" if volume else ""),
            "series_id": make_series_id(series_title),
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
        print(f"  Parse error: {e}")
        return None


def scrape_new_releases(months: int = 3) -> list[dict]:
    """新刊一覧ページをスクレイピングする"""
    session = requests.Session()
    all_comics: list[dict] = []
    seen_ids: set[str] = set()

    # まずトップページにアクセスしてクッキーを取得
    print("Fetching top page...")
    fetch_page(session, BASE_URL)
    time.sleep(1)

    # 月別ページを取得 (今月 + 過去 months ヶ月)
    now = datetime.now()
    pages_to_fetch = [NEW_RELEASE_URL]

    # 月別フィルタが存在する場合の追加URL
    for i in range(months):
        month = (now.month - i - 1) % 12 + 1
        year = now.year - ((now.month - i - 1) // 12)
        pages_to_fetch.append(f"{NEW_RELEASE_URL}?year={year}&month={month:02d}")

    for url in pages_to_fetch:
        print(f"Fetching: {url}")
        soup = fetch_page(session, url)
        if not soup:
            time.sleep(2)
            continue

        # 複数のセレクタでコミックアイテムを検索
        items = (
            soup.select(".product-item")
            or soup.select(".book-item")
            or soup.select(".item")
            or soup.select("li.release-item")
            or soup.select(".new-release-item")
            or soup.select("article")
        )

        print(f"  Found {len(items)} items")

        for item in items:
            comic = parse_comic_item(item)
            if comic and comic["id"] not in seen_ids:
                seen_ids.add(comic["id"])
                all_comics.append(comic)

        # ページネーション
        next_page = soup.select_one("a.next") or soup.select_one(".pagination a[rel='next']")
        page = 2
        while next_page and page <= 10:
            next_url = urljoin(BASE_URL, next_page.get("href", ""))
            if not next_url or next_url in pages_to_fetch:
                break
            print(f"  Fetching page {page}: {next_url}")
            time.sleep(1.5)
            soup = fetch_page(session, next_url)
            if not soup:
                break
            items = (
                soup.select(".product-item")
                or soup.select(".book-item")
                or soup.select(".item")
                or soup.select("li.release-item")
                or soup.select(".new-release-item")
                or soup.select("article")
            )
            for item in items:
                comic = parse_comic_item(item)
                if comic and comic["id"] not in seen_ids:
                    seen_ids.add(comic["id"])
                    all_comics.append(comic)
            next_page = soup.select_one("a.next") or soup.select_one(".pagination a[rel='next']")
            page += 1

        time.sleep(1.5)

    return all_comics


def load_existing(path: Path) -> list[dict]:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                return data.get("comics", [])
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def merge_comics(existing: list[dict], new: list[dict]) -> list[dict]:
    """既存データと新規データをマージ（IDで重複排除）"""
    merged = {c["id"]: c for c in existing}
    for comic in new:
        merged[comic["id"]] = comic
    return list(merged.values())


def save(comics: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 発売日の新しい順にソート
    comics_sorted = sorted(
        comics,
        key=lambda c: c.get("release_date") or "0000-00-00",
        reverse=True,
    )
    payload = {
        "generated_at": datetime.now().isoformat(),
        "count": len(comics_sorted),
        "comics": comics_sorted,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(comics_sorted)} comics to {path}")


def main() -> None:
    print("=== 講談社コミックス新刊スクレイパー ===")
    print(f"Target: {NEW_RELEASE_URL}")
    print()

    new_comics = scrape_new_releases(months=2)
    print(f"\nFetched {len(new_comics)} new comics")

    existing = load_existing(OUTPUT_PATH)
    merged = merge_comics(existing, new_comics)

    save(merged, OUTPUT_PATH)
    print("Done!")


if __name__ == "__main__":
    main()
