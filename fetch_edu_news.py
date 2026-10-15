"""每日教育新聞蒐集器（0–16 歲）
流程：讀取來源 → 去重 → 抓內文 → Claude 判斷相關性、分類、摘要 → 產生網頁
只保存「標題 + 連結 + AI 改寫摘要」，不保存或轉貼原文。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from itertools import zip_longest
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
import html as htmllib

import feedparser
import requests
import trafilatura

try:
    from googlenewsdecoder import gnewsdecoder
except Exception:  # 沒裝或失敗就略過，改用 Google 的轉址連結
    gnewsdecoder = None

TZ = timezone(timedelta(hours=8))  # 台灣時間
ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(ROOT, "sources.json")
DATA_FILE = os.path.join(ROOT, "docs", "data.json")
HTML_FILE = os.path.join(ROOT, "docs", "index.html")
UA = {"User-Agent": "Mozilla/5.0 (compatible; EduNewsBot/1.0)"}

AGES = ["0-3", "3-6", "6-12", "12-16"]
TOPICS = ["政策", "教養", "學習方法", "心理健康", "其他"]

SYSTEM_PROMPT = f"""你是台灣的教育資訊編輯，讀者是 0–16 歲孩子的家長與老師。
請根據提供的文章資訊，只輸出一個 JSON 物件，不要有其他文字。

欄位：
- relevant：布林值。文章主題必須與 0–16 歲兒童、青少年的教育、教養、學習、身心發展或教育政策直接相關才是 true。
  大學／成人進修、純娛樂、商品廣告、與孩子無關的新聞一律 false。
- ages：陣列，從 {AGES} 中選出適用年齡層（可複選）。若適用全年齡，四個都列。
- topic：從 {TOPICS} 擇一。
- summary：繁體中文，最多 3 句話，用你自己的話改寫，不得照抄原文句子。
  只能根據提供的資料寫，不可自行補充或猜測事實。若只有標題、資訊不足，就只寫 1 句，不要硬寫。
- takeaway：一句話，說明家長或老師可以怎麼運用或留意什麼（不超過 40 字）。

若 relevant 為 false，其他欄位可留空字串或空陣列。"""


# ---------- 工具函式 ----------
def now():
    return datetime.now(TZ)


def normalize_url(url):
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")
         and k.lower() not in ("fbclid", "gclid")]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def clean_title(t):
    """去掉 Google 新聞標題尾端的「 - 媒體名」，方便比對重複。"""
    return re.sub(r"\s*[-–—|｜]\s*[^-–—|｜]{1,20}$", "", t or "").strip()


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", htmllib.unescape(s or ""))).strip()


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


# ---------- 蒐集 ----------
def parse_feed(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content)


def collect(cfg):
    """回傳各來源的候選文章清單（list of list），並印出每個來源的狀況。"""
    lookback = timedelta(hours=cfg["settings"].get("lookback_hours", 48))
    cutoff = datetime.now(timezone.utc) - lookback
    groups = []

    jobs = []
    for f in cfg.get("rss_feeds", []):
        if f.get("enabled", True):
            jobs.append((f["name"], f["url"], False))
    for q in cfg.get("google_news_queries", []):
        url = ("https://news.google.com/rss/search?q=" + quote(q + " when:2d")
               + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        jobs.append((f"Google新聞：{q}", url, True))

    for name, url, is_gnews in jobs:
        try:
            feed = parse_feed(url)
        except Exception as e:
            print(f"  ✗ {name}：讀取失敗（{e}）")
            continue
        items = []
        for e in feed.entries:
            pub = getattr(e, "published_parsed", None)
            if pub:
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            source = name
            if is_gnews and getattr(e, "source", None):
                source = e.source.get("title", name)
            items.append({
                "title": strip_html(e.get("title", "")),
                "url": e.get("link", ""),
                "source": source,
                "desc": "" if is_gnews else strip_html(e.get("summary", ""))[:500],
                "gnews": is_gnews,
            })
        print(f"  ✓ {name}：{len(items)} 則")
        groups.append(items)
    return groups


def resolve_gnews(url):
    if gnewsdecoder is None:
        return url
    try:
        res = gnewsdecoder(url, interval=1)
        if res.get("status") and res.get("decoded_url"):
            return res["decoded_url"]
    except Exception:
        pass
    return url


def get_text(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        text = trafilatura.extract(r.content) or ""
        return text[:3000]
    except Exception:
        return ""


# ---------- AI ----------
DEAD_MODELS = set()  # 已確認 404 的模型，本次執行不再嘗試


def analyze(models, api_key, item, text):
    """呼叫 Gemini（免費額度）。依序嘗試模型清單；失敗回傳 None（該文章明天會再試）。"""
    material = f"標題：{item['title']}\n來源：{item['source']}\n"
    if text:
        material += f"內文節錄：\n{text}"
    elif item.get("desc"):
        material += f"來源提供的摘要：{item['desc']}"
    else:
        material += "（只有標題，沒有內文）"

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": material}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": 2000,
        },
    }
    for model in models:
        if model in DEAD_MODELS:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, json=body, timeout=60,
                                  headers={"x-goog-api-key": api_key})
                if r.status_code == 404:  # 模型名稱不存在，換下一個
                    print(f"    模型 {model} 找不到（404），改試清單中的下一個")
                    DEAD_MODELS.add(model)
                    break
                if r.status_code == 429:  # 超過免費額度的速率限制，等一下再試
                    print("    被限速（429），等待 30 秒後重試")
                    time.sleep(30)
                    continue
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                raw = "".join(p.get("text", "") for p in parts)
                m = re.search(r"\{.*\}", raw, re.S)
                return json.loads(m.group(0))
            except Exception as e:
                print(f"    AI 呼叫失敗（{model}，第 {attempt + 1} 次）：{e}")
                time.sleep(5)
    return None


# ---------- 輸出網頁 ----------
PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>0–16 歲教育每日精選</title>
<style>
:root{--bg:#faf8f5;--card:#fff;--text:#2b2b2b;--sub:#6b6b6b;--line:#e8e3dc;--accent:#2f6f5e;--tag:#e6f1ed}
@media (prefers-color-scheme:dark){:root{--bg:#1b1c1b;--card:#252726;--text:#ececec;--sub:#a5a5a5;--line:#3a3d3b;--accent:#7cc7b0;--tag:#2c3a35}}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;line-height:1.7}
.wrap{max-width:760px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--sub);font-size:.9rem;margin:0 0 16px}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.bar button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:4px 12px;cursor:pointer;font-size:.85rem}
.bar button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.day h2{font-size:1.05rem;margin:28px 0 8px;color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:10px 0}
.card h3{font-size:1.02rem;margin:6px 0}
.card a{color:inherit;text-decoration:none}
.card a:hover{text-decoration:underline}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:.8rem}
.tag{background:var(--tag);color:var(--accent);border-radius:6px;padding:1px 8px}
.src{color:var(--sub)}
.card p{margin:6px 0;font-size:.95rem}
.take{color:var(--sub)}
.foot{margin-top:40px;color:var(--sub);font-size:.8rem}
</style>
</head>
<body><div class="wrap">
<h1>0–16 歲教育每日精選</h1>
<p class="sub">最後更新：__UPDATED__　｜　摘要由 AI 整理，內容以原文為準</p>
<div class="bar" id="ageBar"></div>
<div class="bar" id="topicBar"></div>
__BODY__
<p class="foot">本頁僅提供標題、連結與 AI 改寫摘要，著作權屬原媒體所有。</p>
</div>
<script>
const state={age:"全部",topic:"全部"};
function mk(id,key,opts){const bar=document.getElementById(id);
 opts.forEach(o=>{const b=document.createElement("button");b.textContent=o.label;
  if(o.val==="全部")b.className="on";
  b.onclick=()=>{state[key]=o.val;[...bar.children].forEach(c=>c.className="");b.className="on";apply();};
  bar.appendChild(b);});}
mk("ageBar","age",[{label:"全部年齡",val:"全部"},{label:"0–3 歲",val:"0-3"},{label:"3–6 歲",val:"3-6"},{label:"6–12 歲",val:"6-12"},{label:"12–16 歲",val:"12-16"}]);
mk("topicBar","topic",[{label:"全部主題",val:"全部"},...["政策","教養","學習方法","心理健康","其他"].map(t=>({label:t,val:t}))]);
function apply(){
 document.querySelectorAll(".card").forEach(c=>{
  const okA=state.age==="全部"||c.dataset.ages.split(" ").includes(state.age);
  const okT=state.topic==="全部"||c.dataset.topic===state.topic;
  c.style.display=(okA&&okT)?"":"none";});
 document.querySelectorAll(".day").forEach(d=>{
  d.style.display=[...d.querySelectorAll(".card")].some(c=>c.style.display!=="none")?"":"none";});
}
</script>
</body></html>"""


def esc(s):
    return htmllib.escape(s or "", quote=True)


def render(articles):
    by_date = {}
    for a in articles:
        by_date.setdefault(a["date"], []).append(a)
    parts = []
    for d in sorted(by_date, reverse=True):
        cards = []
        for a in by_date[d]:
            ages = " ".join(a["ages"])
            age_tags = "".join(f'<span class="tag">{esc(x.replace("-", "–"))} 歲</span>' for x in a["ages"])
            cards.append(
                f'<article class="card" data-ages="{esc(ages)}" data-topic="{esc(a["topic"])}">'
                f'<div class="meta">{age_tags}<span class="tag">{esc(a["topic"])}</span>'
                f'<span class="src">{esc(a["source"])}</span></div>'
                f'<h3><a href="{esc(a["url"])}" target="_blank" rel="noopener noreferrer">{esc(a["title"])}</a></h3>'
                f'<p>{esc(a["summary"])}</p>'
                f'<p class="take">給家長與老師：{esc(a["takeaway"])}</p></article>'
            )
        parts.append(f'<section class="day"><h2>{esc(d)}</h2>{"".join(cards)}</section>')
    body = "\n".join(parts) or "<p>目前還沒有文章，請稍後再來。</p>"
    page = PAGE.replace("__UPDATED__", now().strftime("%Y-%m-%d %H:%M")).replace("__BODY__", body)
    os.makedirs(os.path.dirname(HTML_FILE), exist_ok=True)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(page)


# ---------- 主流程 ----------
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("找不到 GEMINI_API_KEY，請先在 GitHub Secrets 設定。")

    with open(SOURCES_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    st = cfg["settings"]
    models = st.get("models") or [st.get("model", "gemini-3.1-flash-lite")]
    max_new = st.get("max_new_per_run", 15)
    pause = st.get("seconds_between_calls", 7)  # 控制每分鐘呼叫次數，避免超過免費限速
    keep_days = st.get("keep_days", 60)

    data = {"articles": [], "rejected": []}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)

    today = now().strftime("%Y-%m-%d")
    keep_after = (now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    data["articles"] = [a for a in data["articles"] if a["date"] >= keep_after]
    data["rejected"] = [r for r in data["rejected"] if r["date"] >= keep_after]

    seen_urls = {normalize_url(a["url"]) for a in data["articles"]}
    seen_urls |= {normalize_url(r["url"]) for r in data["rejected"]}
    recent_titles = [clean_title(a["title"]) for a in data["articles"]]
    recent_titles += [r.get("title", "") for r in data["rejected"]]

    print("== 蒐集來源 ==")
    groups = collect(cfg)

    # 各來源輪流取，避免某個來源佔滿名額
    candidates = [x for tup in zip_longest(*groups) for x in tup if x]
    print(f"== 共 {len(candidates)} 則候選，開始去重與分析（上限 {max_new} 則）==")

    processed = added = fails = 0
    for item in candidates:
        if processed >= max_new:
            break
        if fails >= 3:
            print("  連續 3 次 AI 失敗，今天先停止（可能是額度用完），明天會自動再試。")
            break
        if not item["title"] or not item["url"]:
            continue
        ct = clean_title(item["title"])
        if any(similar(ct, t) >= 0.8 for t in recent_titles if t):
            continue
        if normalize_url(item["url"]) in seen_urls:
            continue

        url = resolve_gnews(item["url"]) if item["gnews"] else item["url"]
        if normalize_url(url) in seen_urls:
            continue
        text = get_text(url)
        result = analyze(models, api_key, item, text)
        processed += 1
        time.sleep(pause)

        if result is None:  # AI 失敗：不記錄，讓明天可以重試
            fails += 1
            continue
        fails = 0
        recent_titles.append(ct)
        seen_urls.add(normalize_url(url))

        if not result.get("relevant"):
            data["rejected"].append({"url": url, "title": ct, "date": today})
            continue

        ages = [a for a in result.get("ages", []) if a in AGES] or AGES
        topic = result.get("topic") if result.get("topic") in TOPICS else "其他"
        data["articles"].append({
            "date": today,
            "title": item["title"],
            "url": url,
            "source": item["source"],
            "ages": ages,
            "topic": topic,
            "summary": (result.get("summary") or "").strip(),
            "takeaway": (result.get("takeaway") or "").strip(),
        })
        added += 1
        print(f"  + {item['title'][:40]}")

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    render(data["articles"])
    print(f"== 完成：分析 {processed} 則，新增 {added} 則，網頁已更新 ==")


if __name__ == "__main__":
    main()
