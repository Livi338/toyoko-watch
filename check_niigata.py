#!/usr/bin/env python3
"""
東橫INN 新潟兩館 空房監控（非會員、不需帳密）

目標：東橫INN新潟駅前 (00060) / 東橫INN新潟古町 (00186)
日期：入住 2027-08-01 ~ 2027-08-04（每晚各查一次，任一晚有房就通知）
通知：Discord Webhook

用法：
    python check_niigata.py            # 正常執行
    python check_niigata.py --probe    # 除錯：存下每頁截圖，確認判斷是否正確
"""

import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────────────────────

HOTELS = {
    "00060": "東橫INN新潟駅前",
    "00186": "東橫INN新潟古町",
}

CHECKIN_DATES = ["2027-08-01", "2027-08-02", "2027-08-03"]

PEOPLE = 1
ROOMS = 1
SMOKING = "noSmoking"   # 只要禁菸房；想連吸菸房一起查，見 README

# 你手動查詢時取得的真實網址格式
SEARCH_URL = (
    "https://www.toyoko-inn.com/china/search/result/room_plan/"
    "?hotel={hotel}&people={people}&room={room}&smoking={smoking}"
    "&start={start}&end={end}"
)

# 滿房時頁面會出現的字（例：「經濟單人房沒有空房。」）
SOLD_OUT_TEXT = "沒有空房"

# 有房時會出現價格；這幾種寫法任一命中就算有價格
PRICE_RE = re.compile(r"(¥|￥|JPY)\s*[\d,]{3,}|[\d,]{3,}\s*(日圓|日元|円)")

STATE_FILE = Path("state_niigata.json")
TIMEOUT_MS = 45_000

# ─────────────────────────────────────────────────────────────

WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
PROBE = "--probe" in sys.argv


def log(msg):
    print(msg, flush=True)


def notify(title, body, color=0x2ECC71):
    if not WEBHOOK:
        log(f"[no webhook] {title}: {body}")
        return
    try:
        requests.post(
            WEBHOOK,
            json={
                "username": "新潟 空房監控",
                "embeds": [{"title": title, "description": body[:3800], "color": color}],
            },
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        log(f"Discord 通知失敗: {e}")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def build_url(hotel_cd, checkin):
    d = date.fromisoformat(checkin)
    return SEARCH_URL.format(
        hotel=hotel_cd, people=PEOPLE, room=ROOMS, smoking=SMOKING,
        start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(),
    )


def classify(text):
    """available / soldout / unknown"""
    if PRICE_RE.search(text):
        return "available"
    if SOLD_OUT_TEXT in text:
        return "soldout"
    return "unknown"


def check_one(page, hotel_cd, checkin):
    url = build_url(hotel_cd, checkin)
    page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
    # 等結果載入：出現「沒有空房」或價格其中之一
    try:
        page.wait_for_function(
            "() => /沒有空房|¥|￥|日圓|円/.test(document.body.innerText)",
            timeout=15_000,
        )
    except Exception:
        pass
    text = page.inner_text("body")

    if PROBE:
        tag = f"{hotel_cd}_{checkin}"
        page.screenshot(path=f"probe_{tag}.png", full_page=True)
        Path(f"probe_{tag}.txt").write_text(text, encoding="utf-8")

    return classify(text), url


def main():
    if not WEBHOOK and not PROBE:
        sys.exit("錯誤：請設定環境變數 DISCORD_WEBHOOK")

    state = load_state()
    hits, unknowns = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            locale="zh-TW",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
            ),
        ).new_page()

        for hotel_cd, name in HOTELS.items():
            for checkin in CHECKIN_DATES:
                key = f"{hotel_cd}:{checkin}"
                try:
                    status, url = check_one(page, hotel_cd, checkin)
                except Exception as e:
                    log(f"❌ {name} {checkin}: {e}")
                    status, url = "unknown", build_url(hotel_cd, checkin)

                log(f"{name} {checkin} 入住: {status}")

                if status == "available" and state.get(key) != "available":
                    hits.append((name, checkin, url))
                if status == "unknown":
                    unknowns.append(key)
                else:
                    state[key] = status   # unknown 不覆寫，避免誤判造成重複通知

                time.sleep(random.uniform(3, 6))  # 禮貌性間隔，不要拿掉

        browser.close()

    if hits and not PROBE:
        body = "\n\n".join(f"**{n}** — {c} 入住\n{u}" for n, c, u in hits)
        notify("🏨 新潟有空房了！快去訂！", body, color=0x2ECC71)

    total = len(HOTELS) * len(CHECKIN_DATES)
    if len(unknowns) == total and not PROBE:
        if state.get("_warned") != "yes":
            notify(
                "⚠️ 新潟監控可能失效",
                "所有查詢都判斷不出結果，網站版面可能改了。請用 probe 模式檢查截圖。",
                color=0xE67E22,
            )
            state["_warned"] = "yes"
    else:
        state["_warned"] = "no"

    # 每月心跳
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("_heartbeat") != month and not PROBE:
        ok = total - len(unknowns)
        notify("💤 新潟監控運作中", f"{month} 月報：{ok}/{total} 筆查詢正常判讀。",
               color=0x95A5A6)
        state["_heartbeat"] = month

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
