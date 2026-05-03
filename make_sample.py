#!/usr/bin/env python3
"""
OpenBD からランダムに講談社コミックを取得してサンプルデータを生成するスクリプト。
ユーザー環境で一度実行すると data/comics.json と data/comics.js が更新される。

実行: python make_sample.py
"""
import json, hashlib, re, time
from datetime import datetime
from pathlib import Path
import requests

# 講談社の有名コミックの実在ISBN（2020-2024年頃の既刊）
KNOWN_ISBNS = [
    # ブルーロック
    "9784065272398","9784065285510","9784065296929","9784065307465",
    "9784065318478","9784065330807","9784065344316","9784065353882",
    "9784065367919","9784065378816","9784065389003",
    # 進撃の巨人
    "9784063963625","9784063964462","9784063965179",
    # 東京卍リベンジャーズ
    "9784063960907","9784063961966","9784063962710","9784063964615",
    "9784063965186","9784063966046","9784063966787",
    # 五等分の花嫁
    "9784063960075","9784063960778","9784063961461","9784063962390",
    "9784063963403","9784063964325",
    # シャングリラ・フロンティア
    "9784065316894","9784065330838","9784065344842","9784065358726",
    "9784065370384","9784065382431",
    # スキップとローファー
    "9784063968071","9784063969474","9784063970630","9784063973303",
    "9784065300275","9784065314876","9784065329160","9784065343082",
    "9784065360354","9784065374499",
    # ダンジョン飯
    "9784047303157","9784047305656","9784047307209","9784047308886",
    "9784047311855","9784047314207","9784047316621","9784047319257",
    "9784047321571","9784047323273","9784047325372","9784047327604",
    "9784047329393",
    # ヴィンランド・サガ
    "9784063965520","9784063966565","9784063967234","9784063967241",
    "9784065318508","9784065344323","9784065371763",
    # アオアシ
    "9784091280398","9784091281449","9784091282651","9784091284679",
    "9784091286949","9784091288851","9784091291578","9784091293756",
    # 彼女、お借りします
    "9784063960211","9784063961157","9784063962025","9784063962826",
    "9784063963700","9784063964530","9784063965384","9784063966238",
    "9784063967142","9784063967890","9784063968712","9784063969610",
    "9784065301005","9784065311258","9784065322987","9784065334217",
    "9784065345474","9784065356791","9784065367636","9784065378854",
    # ノラガミ
    "9784063963403","9784063964431","9784063965438","9784063966558",
    "9784063967579","9784063968590","9784063969559","9784065300909",
    # カッコウの許嫁
    "9784065207840","9784065221952","9784065234426","9784065245613",
    "9784065256602","9784065267432","9784065278827","9784065290415",
    "9784065302279","9784065313849","9784065325040","9784065336824",
    "9784065347638","9784065359427",
    # 忘却バッテリー
    "9784065271063","9784065283844","9784065295335","9784065307243",
    "9784065319154","9784065331170","9784065344347","9784065356401",
    "9784065367941","9784065379301",
    # 七つの大罪
    "9784063963250","9784063964119","9784063964980","9784063965872",
    "9784063966726","9784063967593","9784063968445","9784063969369",
]

def normalize_isbn(s):
    return re.sub(r"[^0-9X]", "", str(s).upper())

def parse_volume(title):
    patterns = [
        r"^(.+?)[\s　]*[（(](\d+)[）)]\s*$",
        r"^(.+?)[\s　]+第?(\d+)巻?\s*$",
    ]
    for pat in patterns:
        m = re.match(pat, title.strip())
        if m:
            return m.group(1).strip(), m.group(2)
    return title.strip(), ""

def parse_date(text):
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", str(text))
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""

def make_id(title, volume=""):
    sid = hashlib.md5(re.sub(r"\s+","",title).encode()).hexdigest()[:12]
    return sid + (f"_{volume}" if volume else ""), sid

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def fetch_openbd_batch(isbns):
    resp = requests.get(
        "https://api.openbd.jp/v1/get",
        params={"isbn": ",".join(isbns)},
        timeout=30,
        headers={"User-Agent": BROWSER_UA},
    )
    resp.raise_for_status()
    return resp.json()

def build_comics():
    print(f"Fetching {len(KNOWN_ISBNS)} ISBNs from OpenBD...")
    all_items = []
    batch_size = 50
    for i in range(0, len(KNOWN_ISBNS), batch_size):
        batch = KNOWN_ISBNS[i:i+batch_size]
        try:
            items = fetch_openbd_batch(batch)
            all_items.extend(items)
        except Exception as e:
            print(f"  Warning: batch {i//batch_size + 1} failed ({e}), skipping")
            all_items.extend([None] * len(batch))
        print(f"  Fetched {min(i+batch_size, len(KNOWN_ISBNS))}/{len(KNOWN_ISBNS)}")
        time.sleep(0.5)

    comics = []
    for item in all_items:
        if not item:
            continue
        s = item.get("summary", {})
        onix = item.get("onix", {})

        title_raw = s.get("title", "")
        if not title_raw:
            continue

        series_title, volume = parse_volume(title_raw)
        cid, sid = make_id(series_title, volume)

        # 著者（OpenBDのsummary.authorは「姓, 名, 生年-」形式なのでクリーンアップ）
        def clean_author(raw: str) -> str:
            # "姓, 名, 1960-" → "姓名" に変換
            parts = [p.strip().rstrip("-").strip() for p in raw.split(",")]
            # 末尾が年号（数字）のパートは除去
            parts = [p for p in parts if p and not re.match(r"^\d{4}$", p)]
            return "".join(parts)

        authors = []
        for cont in onix.get("DescriptiveDetail", {}).get("Contributor", []):
            name = cont.get("PersonName", {}).get("content", "")
            if name:
                authors.append(clean_author(name))
        author = "／".join(authors) if authors else clean_author(s.get("author", ""))

        # 発売日
        supply = onix.get("ProductSupply", {})
        supply_detail = supply.get("SupplyDetail", {}) if isinstance(supply, dict) else {}
        on_sale = supply_detail.get("OnSaleDate", "") if isinstance(supply_detail, dict) else ""
        pub_date = onix.get("PublishingDetail", {}).get("PublishingDate", [])
        date_str = on_sale or (pub_date[0].get("Date","") if pub_date else "")
        release_date = parse_date(date_str) if date_str else parse_date(s.get("pubdate",""))

        # レーベル（OpenBD ONIXのCollectionはdict、CollectionTypeは文字列）
        coll = onix.get("DescriptiveDetail", {}).get("Collection", {})
        label = ""
        if isinstance(coll, dict) and coll.get("CollectionType") == "10":
            for ti in coll.get("TitleDetail", []):
                for tel in ti.get("TitleElement", []):
                    label = tel.get("TitleText", {}).get("content", "") or label

        isbn = normalize_isbn(s.get("isbn", ""))
        # OpenBDはsummary.coverが空でもISBNからカバーURLを構築できる
        cover_url = s.get("cover", "") or (f"https://cover.openbd.jp/{isbn}.jpg" if isbn else "")

        comics.append({
            "id":           cid,
            "series_id":    sid,
            "series_title": series_title,
            "full_title":   title_raw,
            "volume":       volume,
            "author":       author,
            "release_date": release_date,
            "cover_url":    cover_url,
            "detail_url":   "",
            "label":        label,
            "isbn":         isbn,
        })

    return comics

def save(comics):
    out = Path(__file__).parent / "data"
    out.mkdir(exist_ok=True)
    sorted_c = sorted(comics, key=lambda c: c.get("release_date") or "0000-00-00", reverse=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "count": len(sorted_c),
        "comics": sorted_c,
    }
    (out / "comics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "comics.js").write_text(
        "var COMICS_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"Saved {len(sorted_c)} comics")

if __name__ == "__main__":
    comics = build_comics()
    save(comics)
    print("Done! Open index.html to view.")
