#!/usr/bin/env python3
"""
東橫INN長岡駅前 開賣公告監控
監控的是「公告有沒有出現」，不是「有沒有空房」。
全部都是公開頁面，不需要登入、不需要瀏覽器、不需要任何帳密。
"""

import json
import os
import re
from pathlib import Path

import requests

SOURCES = [
    ("東橫INN 企業新聞", "https://www.toyoko-inn.co.jp/news/"),
    ("官網 新潟縣分館列表", "https://www.toyoko-inn.com/search/area/niigata/"),
    ("官網 飯店列表", "https://www.toyoko-inn.com/hotel_list/"),
]

KEYWORDS = ["長岡駅前", "長岡駅", "長岡"]
BOOKING_SIGNALS = ["予約開始", "予約受付", "宿泊予約", "オープン"]

STATE_FILE = Path("state_nagaoka.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
STRIP_RE = re.compile(r"<[^>]+>")


def log(msg):
    print(msg, flush=True)


def notify(title, body, color=0x3498DB):
    if not WEBHOOK:
        log(f"[no webhook] {title}: {body}")
        return
    try:
        requests.post(
            WEBHOOK,
            json={
                "username": "長岡駅前 開賣監控",
                "embeds": [{"title": title, "description": body[:3800], "color": color}],
            },
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        log(f"Discord 通知失敗: {e}")


def to_text(html):
    html = TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", STRIP_RE.sub(" ", html))


def context(text, keyword, span=120):
    i = text.find(keyword)
    if i < 0:
        return ""
    return text[max(0, i - span): i + span].strip()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def main():
    if not WEBHOOK:
        log("警告：沒有 DISCORD_WEBHOOK，只會印在畫面上")

    state = load_state()
    hits, errors = [], []

    for name, url in SOURCES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            text = to_text(r.text)
        except Exception as e:
            log(f"❌ {name}: {e}")
            errors.append(name)
            continue

        found = [k for k in KEYWORDS if k in text]
        if not found:
            log(f"   {name}: 尚未出現")
            state[url] = ""
            continue

        kw = found[0]
        snippet = context(text, kw)
        bookable = any(s in snippet for s in BOOKING_SIGNALS)

        if state.get(url) == snippet:
            log(f"   {name}: 已命中但內容未變，不重複通知")
            continue

        log(f"🔔 {name}: 命中「{kw}」")
        hits.append((name, url, snippet, bookable))
        state[url] = snippet

    if hits:
        urgent = any(h[3] for h in hits)
        body = "\n\n".join(
            f"**{name}**\n{snippet}\n{url}" for name, url, snippet, _ in hits
        )
        notify(
            "🎆 長岡駅前有動靜了！" if urgent else "👀 長岡駅前 頁面出現變化",
            body,
            color=0xE74C3C if urgent else 0x3498DB,
        )

    if len(errors) == len(SOURCES):
        notify("⚠️ 監控異常", "所有來源都抓取失敗，請確認網址是否還有效。", color=0xE67E22)

        # 每月心跳：讓你知道腳本還活著
    from datetime import datetime, timezone
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("_heartbeat") != this_month:
        ok = len(SOURCES) - len(errors)
        notify(
            "💤 監控運作中",
            f"{this_month} 月報：{ok}/{len(SOURCES)} 個來源正常，長岡尚未開賣。",
            color=0x95A5A6,
        )
        state["_heartbeat"] = this_month
    
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
