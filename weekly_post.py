"""每週精選：從過去 7 天蒐集的文章中，依「被幾家媒體報導」挑出最受關注的 3 則，
並改寫成學校可發佈的貼文草稿（附出處與連結），用 Email 寄給自己。
草稿一律需要人工審稿後才發佈。
注意：儲存庫是公開的，所以草稿與收件信箱都不寫進儲存庫，信箱放在 GitHub Secrets。
"""
import html as htmllib
import json
import os
import re
import smtplib
import sys
import time
from datetime import timedelta
from email.message import EmailMessage

import requests

from fetch_edu_news import DATA_FILE, ROOT, SOURCES_FILE, clean_title, get_text, now

CONF_FILE = os.path.join(ROOT, "weekly_config.json")

DEFAULTS = {
    "school_name": "本校",
    "audience": "家長與老師",
    "tone": "溫暖、清楚、不說教，像學校平常寫給家長的通知與分享",
    "post_length": "200 到 350 字",
    "top_n": 3,
    "candidate_pool": 10,
    "hashtags": ["#教育", "#親子教養"],
    "seconds_between_calls": 7,
}


def load_conf():
    conf = dict(DEFAULTS)
    if os.path.exists(CONF_FILE):
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
    return conf


def load_models():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        st = json.load(f)["settings"]
    return st.get("models") or [st.get("model", "gemini-3.1-flash-lite")]


def esc(s):
    return htmllib.escape(s or "", quote=True)


# ---------- Gemini ----------
DEAD = set()


def call_gemini(models, api_key, system, user, pause):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
            "maxOutputTokens": 3000,
        },
    }
    for model in models:
        if model in DEAD:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, json=body, timeout=90,
                                  headers={"x-goog-api-key": api_key})
                if r.status_code == 404:
                    print(f"    模型 {model} 找不到（404），改試下一個")
                    DEAD.add(model)
                    break
                if r.status_code == 429:
                    print("    被限速（429），等待 30 秒後重試")
                    time.sleep(30)
                    continue
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                raw = "".join(p.get("text", "") for p in parts)
                m = re.search(r"\{.*\}", raw, re.S)
                result = json.loads(m.group(0))
                time.sleep(pause)
                return result
            except Exception as e:
                print(f"    AI 呼叫失敗（{model}，第 {attempt + 1} 次）：{e}")
                time.sleep(5)
    return None


# ---------- 聲量與挑選 ----------
def outlets(a):
    return [a["source"]] + [x for x in a.get("also_from", []) if x != a["source"]]


def buzz(a):
    return len(set(outlets(a)))


def pick_top(models, api_key, pool, conf):
    """先用「報導家數」排序取前幾名，再請 AI 在其中挑出最適合學校發佈的 N 則。失敗就直接取前 N 名。"""
    n = conf["top_n"]
    fallback = list(range(min(n, len(pool))))
    if len(pool) <= n:
        return [(i, "") for i in fallback]

    lines = []
    for i, a in enumerate(pool):
        lines.append(
            f"[{i}] 報導家數：{buzz(a)}｜年齡層：{'、'.join(a['ages'])}｜主題：{a['topic']}\n"
            f"    標題：{clean_title(a['title'])}\n    摘要：{a['summary']}"
        )
    system = (
        f"你是{conf['school_name']}的公關編輯。以下是本週蒐集的教育文章，已依「被幾家媒體報導」"
        f"（代表網路關注度）由高到低排列。請挑出 {n} 則最適合學校轉述給{conf['audience']}的文章。\n"
        "挑選原則：1) 報導家數是第一優先；2) 家數接近時，優先選對家長與老師實用、"
        "年齡層與主題彼此不重複的；3) 避免涉及政黨立場爭議、商品導購或廣告性質的文章。\n"
        '只輸出 JSON：{"picks":[{"index":數字,"reason":"一句話說明為何入選"}]}'
    )
    res = call_gemini(models, api_key, system, "\n\n".join(lines), conf["seconds_between_calls"])
    picks, seen = [], set()
    if res:
        for p in res.get("picks", []):
            idx = p.get("index")
            if isinstance(idx, int) and 0 <= idx < len(pool) and idx not in seen:
                seen.add(idx)
                picks.append((idx, p.get("reason", "")))
    for i in fallback:  # AI 少挑或失敗時，用報導家數排名補足
        if len(picks) >= n:
            break
        if i not in seen:
            seen.add(i)
            picks.append((i, ""))
    return picks[:n]


# ---------- 改寫貼文 ----------
def write_post(models, api_key, a, conf):
    text = get_text(a["url"])
    system = (
        f"你是{conf['school_name']}的公關編輯，要把一則教育新聞改寫成學校社群貼文草稿，讀者是{conf['audience']}。\n"
        f"語氣：{conf['tone']}。\n"
        "規則：\n"
        "1. 只能使用提供資料中的事實，不得補充、推測或編造數據、人名、研究結果。資料不足就少寫，不要硬寫。\n"
        "2. 用自己的話重新組織，不得照抄原文句子，也不要沿用原文的標題與段落結構。\n"
        "3. 結構：第一句點出「這件事和孩子或家長有什麼關係」；接著 2 到 4 個短段落整理重點；"
        "最後給一個家長或老師可以做的實用小提醒。\n"
        f"4. 長度約 {conf['post_length']}。口語、好讀，可適度使用 1 到 3 個表情符號。\n"
        "5. 不要寫出處與連結，也不要加 hashtag（由程式附加）。不替學校做政策承諾。\n"
        '只輸出 JSON：{"post":"貼文內文","needs_check":布林值,"check_note":"發佈前需要核對什麼"}\n'
        "若提供的資料只有摘要、內文不完整，或內容涉及數據與研究結果，needs_check 設為 true 並說明。"
    )
    if text:
        user = f"標題：{clean_title(a['title'])}\n來源：{a['source']}\n適用年齡：{'、'.join(a['ages'])}\n內文節錄：\n{text}"
    else:
        user = (f"標題：{clean_title(a['title'])}\n來源：{a['source']}\n適用年齡：{'、'.join(a['ages'])}\n"
                f"（無法取得原文，只有以下整理）\n摘要：{a['summary']}\n重點：{a['takeaway']}")
    res = call_gemini(models, api_key, system, user, conf["seconds_between_calls"])
    if not res or not res.get("post"):
        return None
    needs_check = bool(res.get("needs_check")) or not text
    note = (res.get("check_note") or "").strip()
    if not text:
        note = "無法取得原文，本稿只依摘要撰寫，發佈前務必閱讀原文。" + (" " + note if note else "")

    tags = " ".join(conf["hashtags"] + [f"#{a['topic']}"])
    full = (
        f"{res['post'].strip()}\n\n"
        f"📎 原文出處：{a['source']}《{clean_title(a['title'])}》\n{a['url']}\n"
        f"（本則為學校依原文重點整理，完整內容請閱讀原文。）\n\n{tags}"
    )
    return {"text": full, "needs_check": needs_check, "note": note}


# ---------- 寄信 ----------
def build_email(results, conf):
    """回傳 (純文字版, HTML 版)。信件裡不能用 JavaScript，所以沒有複製按鈕，直接選取文字複製即可。"""
    stamp = now().strftime("%Y-%m-%d")
    text_parts = [
        f"{conf['school_name']} 每週精選貼文草稿（{stamp}）",
        "發佈前請務必：1) 打開原文核對事實與數字  2) 依學校用字與立場調整語氣  3) 保留出處與連結，不要使用原文圖片。",
        "「報導家數」是以不同媒體報導同一件事的數量估算網路關注度，並非社群平台的按讚數或留言數。",
    ]
    html_parts = []
    for rank, (a, reason, post) in enumerate(results, 1):
        title = clean_title(a["title"])
        names = "、".join(outlets(a))
        buzz_txt = f"{buzz(a)} 家媒體報導" if buzz(a) >= 2 else "單一來源"
        head = f"第 {rank} 則：{title}"
        meta = f"{buzz_txt}｜{'、'.join(a['ages'])} 歲｜{a['topic']}｜報導媒體：{names}"

        text_parts.append("=" * 30 + f"\n{head}\n{meta}")
        if reason:
            text_parts.append(f"入選原因：{reason}")
        h = [f'<h2 style="font-size:16px;color:#2f6f5e;margin:24px 0 4px">{esc(head)}</h2>',
             f'<div style="color:#6b6b6b;font-size:13px">{esc(meta)}</div>']
        if reason:
            h.append(f'<div style="color:#6b6b6b;font-size:13px">入選原因：{esc(reason)}</div>')
        if post:
            if post["needs_check"]:
                text_parts.append(f"【發佈前請核對】{post['note']}")
                h.append(f'<div style="background:#fff3e0;border-left:4px solid #e0a040;padding:6px 10px;margin:8px 0;font-size:13px">'
                         f'⚠ 發佈前請核對：{esc(post["note"])}</div>')
            text_parts.append("----- 貼文草稿 -----\n" + post["text"])
            h.append('<div style="background:#faf8f5;border:1px solid #e8e3dc;border-radius:8px;padding:12px;margin:8px 0;'
                     'font-size:15px;line-height:1.7">' + esc(post["text"]).replace("\n", "<br>") + "</div>")
        else:
            text_parts.append("這則的草稿產生失敗，請參考原文自行撰寫，或下週再試。\n原文：" + a["url"])
            h.append(f'<div style="background:#fff3e0;padding:6px 10px;font-size:13px">這則的草稿產生失敗，'
                     f'請參考<a href="{esc(a["url"])}">原文</a>自行撰寫，或下週再試。</div>')
        html_parts.append("\n".join(h))

    html_body = (
        '<div style="max-width:640px;margin:0 auto;font-family:system-ui,\'Noto Sans TC\',\'Microsoft JhengHei\',sans-serif;color:#2b2b2b">'
        f'<h1 style="font-size:20px;margin-bottom:4px">{esc(conf["school_name"])} 每週精選貼文草稿</h1>'
        f'<div style="color:#6b6b6b;font-size:13px">{stamp}</div>'
        '<div style="background:#fff3e0;border:1px solid #e0a040;border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13px">'
        "<b>發佈前請務必：</b>① 打開原文核對事實與數字　② 依學校用字與立場調整語氣　③ 保留出處與連結，不要使用原文圖片。<br>"
        "「報導家數」是以不同媒體報導同一件事的數量估算網路關注度，並非社群平台的按讚數或留言數。</div>"
        + "\n".join(html_parts) + "</div>"
    )
    return "\n\n".join(text_parts), html_body


def send_email(subject, text, html_body, conf):
    user = os.environ.get("MAIL_USER", "").strip()
    password = os.environ.get("MAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = [x.strip() for x in os.environ.get("MAIL_TO", "").split(",") if x.strip()]
    if not (user and password and to):
        sys.exit("找不到信箱設定：請在 GitHub Secrets 設定 MAIL_USER、MAIL_APP_PASSWORD、MAIL_TO。")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{conf['school_name']} 每週精選 <{user}>"
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("找不到 GEMINI_API_KEY，請先在 GitHub Secrets 設定。")
    conf = load_conf()
    models = load_models()
    stamp = now().strftime("%Y-%m-%d")
    subject = f"【{conf['school_name']}】每週精選貼文草稿 {stamp}"

    articles = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            articles = json.load(f)["articles"]

    week_ago = (now() - timedelta(days=7)).strftime("%Y-%m-%d")
    pool = [a for a in articles if a["date"] > week_ago]
    pool.sort(key=lambda a: (buzz(a), a["date"]), reverse=True)
    pool = pool[:conf["candidate_pool"]]
    print(f"== 過去 7 天共 {len(pool)} 則候選 ==")
    if not pool:
        send_email(subject, "過去 7 天沒有蒐集到文章，這週沒有草稿。請檢查每日蒐集是否正常。",
                   "<p>過去 7 天沒有蒐集到文章，這週沒有草稿。請檢查每日蒐集是否正常。</p>", conf)
        print("== 已寄出「本週無草稿」通知 ==")
        return

    picks = pick_top(models, api_key, pool, conf)
    results = []
    for idx, reason in picks:
        a = pool[idx]
        print(f"  撰寫：{clean_title(a['title'])[:40]}（{buzz(a)} 家報導）")
        results.append((a, reason, write_post(models, api_key, a, conf)))

    text, html_body = build_email(results, conf)
    send_email(subject, text, html_body, conf)
    print(f"== 完成：已寄出 {len(results)} 則草稿 ==")


if __name__ == "__main__":
    main()
