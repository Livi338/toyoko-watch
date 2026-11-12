#!/usr/bin/env python3
"""
東橫INN 空房監控（非會員、不需帳密，可同時監控多趟行程）

每一晚都以「住 1 晚」查詢，任一飯店任一晚有房就發 Discord 通知。
入住日已經過去的日期會自動跳過，行程結束後不用手動刪除。

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
# 設定區：要新增行程，照格式多加一個 { ... } 區塊即可
# dates 填「入住日」，例如住 12/12、12/13、12/14 三晚（12/15 退房）
# ─────────────────────────────────────────────────────────────

TRIPS = [
    {
        "name": "長岡花火（新潟）",
        "hotels": {
            "00060": "東橫INN新潟駅前",
            "00186": "東橫INN新潟古町",
        },
        "dates": ["2027-08-01", "2027-08-02", "2027-08-03"],
        "people": 1,
    },
    {
        "name": "東京 12月",
        "hotels": {
            "00269": "東橫INN東京駅八重洲北口",
            "00170": "東橫INN日本橋三越前A4",
            "00095": "東橫INN日本橋人形町",
            "00156": "東橫INN日本橋浜町明治座前",
        },
        "dates": ["2026-12-12", "2026-12-13", "2026-12-14"],
        "people": 1,
        # 至少要有一間能「連住幾晚」才通知；設 1 = 零散單晚也通知
        "min_streak": 1,
    },
]

ROOMS = 1
SMOKING = "noSmoking"   # 只查禁菸房

SEARCH_URL = (
    "https://www.toyoko-inn.com/china/search/result/room_plan/"
    "?hotel={hotel}&people={people}&room={room}&smoking={smoking}"
    "&start={start}&end={end}"
)

SOLD_OUT_TEXT = "沒有空房"
PRICE_RE = re.compile(r"(¥|￥|JPY)\s*[\d,]{3,}|[\d,]{3,}\s*(日圓|日元|円)")

STATE_FILE = Path("state_niigata.json")
TIMEOUT_MS = 45_000
JST = timezone(timedelta(hours=9))

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
                "username": "東橫INN 空房監控",
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


def build_url(hotel_cd, checkin, people):
    d = date.fromisoformat(checkin)
    return SEARCH_URL.format(
        hotel=hotel_cd, people=people, room=ROOMS, smoking=SMOKING,
        start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(),
    )


def classify(text):
    """available / soldout / unknown"""
    if PRICE_RE.search(text):
        return "available"
    if SOLD_OUT_TEXT in text:
        return "soldout"
    return "unknown"


def streaks(avail_dates, trip_dates):
    """把有房的日期切成連續區段，回傳 [(起, 迄), ...]，長的排前面。"""
    runs, cur = [], []
    for d in sorted(trip_dates):
        if d in avail_dates:
            cur.append(d)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return sorted(((r[0], r[-1]) for r in runs),
                  key=lambda r: -(date.fromisoformat(r[1]) - date.fromisoformat(r[0])).days)


def nights(run):
    return (date.fromisoformat(run[1]) - date.fromisoformat(run[0])).days + 1


def md(d):
    x = date.fromisoformat(d)
    return f"{x.month}/{x.day}"


def summarize(trip, avail, people, today):
    """avail: {hotel_cd: set(有房的入住日)} → (標題, 內文, 最長連住晚數)"""
    dates = [d for d in trip["dates"] if date.fromisoformat(d) >= today]
    full, multi, single = [], [], []
    best = 0
    first_choice_full = False
    for rank, (cd, name) in enumerate(trip["hotels"].items()):
        label = "⭐第一志願" if rank == 0 else f"備案{rank}"
        for run in streaks(avail.get(cd, set()), dates):
            n = nights(run)
            best = max(best, n)
            end = (date.fromisoformat(run[1]) + timedelta(days=1)).isoformat()
            url = SEARCH_URL.format(hotel=cd, people=people, room=ROOMS,
                                    smoking=SMOKING, start=run[0], end=end)
            span = md(run[0]) if n == 1 else f"{md(run[0])}–{md(run[1])}"
            line = (f"**{name.replace('東橫INN', '')}**（{label}）"
                    f" {span}（{n} 晚）\n{url}")
            item = (-n, rank, line)   # 先比連住晚數，同晚數再比志願
            if n == len(dates):
                full.append(item)
                first_choice_full |= rank == 0
            elif n > 1:
                multi.append(item)
            else:
                single.append(item)
    full, multi, single = ([x[2] for x in sorted(g)] for g in (full, multi, single))

    covered = set().union(*avail.values()) if avail else set()
    gaps = [md(d) for d in dates if d not in covered]

    parts = []
    if full:
        parts.append(f"🟢 **同一間住滿 {len(dates)} 晚，不用搬家**\n" + "\n".join(full))
    if multi:
        parts.append("🟡 **可連住多晚（搭配其他飯店，換一次房）**\n" + "\n".join(multi))
    if single:
        parts.append("⚪ **單晚**\n" + "\n".join(single))
    if gaps:
        parts.append("⛔ 目前四間都沒房的晚上：" + "、".join(gaps))

    if first_choice_full:
        title = f"🟢⭐ {trip['name']}：第一志願可一次訂齊 {len(dates)} 晚！"
    elif full:
        title = f"🟢 {trip['name']}：有飯店可一次訂齊 {len(dates)} 晚！"
    elif best > 1:
        title = f"🟡 {trip['name']}：最多可連住 {best} 晚"
    else:
        title = f"⚪ {trip['name']}：只有零散單晚"
    return title, "\n\n".join(parts), best


def check_one(page, url, tag):
    page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
    try:
        page.wait_for_function(
            "() => /沒有空房|¥|￥|日圓|円/.test(document.body.innerText)",
            timeout=15_000,
        )
    except Exception:
        pass
    text = page.inner_text("body")

    if PROBE:
        page.screenshot(path=f"probe_{tag}.png", full_page=True)
        Path(f"probe_{tag}.txt").write_text(text, encoding="utf-8")

    return classify(text)


def main():
    if not WEBHOOK and not PROBE:
        sys.exit("錯誤：請設定環境變數 DISCORD_WEBHOOK")

    state = load_state()
    today = datetime.now(JST).date()
    changed = set()    # 有新空房出現的行程
    unknowns, total = [], 0

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

        for trip in TRIPS:
            people = trip.get("people", 1)
            for hotel_cd, name in trip["hotels"].items():
                for checkin in trip["dates"]:
                    if date.fromisoformat(checkin) < today:
                        continue  # 已經過去的日期自動跳過

                    total += 1
                    key = f"{hotel_cd}:{checkin}"
                    url = build_url(hotel_cd, checkin, people)
                    try:
                        status = check_one(page, url, f"{hotel_cd}_{checkin}")
                    except Exception as e:
                        log(f"❌ {name} {checkin}: {e}")
                        status = "unknown"

                    log(f"[{trip['name']}] {name} {checkin} 入住: {status}")

                    if status == "available" and state.get(key) != "available":
                        changed.add(trip["name"])
                    if status == "unknown":
                        unknowns.append(key)
                    else:
                        state[key] = status

                    time.sleep(random.uniform(3, 6))  # 禮貌性間隔，不要拿掉

        browser.close()

    if not PROBE:
        for trip in TRIPS:
            if trip["name"] not in changed:
                continue
            # 用最新狀態整理出「這趟目前所有有房的組合」，不只列新出現的
            avail = {}
            for cd in trip["hotels"]:
                ok = {d for d in trip["dates"] if state.get(f"{cd}:{d}") == "available"}
                if ok:
                    avail[cd] = ok
            title, body, best = summarize(trip, avail, trip.get("people", 1), today)
            if best >= trip.get("min_streak", 1):
                color = 0x2ECC71 if title.startswith("🟢") else (
                    0xF1C40F if title.startswith("🟡") else 0x95A5A6)
                notify(title, body, color=color)

        if total and len(unknowns) == total:
            if state.get("_warned") != "yes":
                notify(
                    "⚠️ 空房監控可能失效",
                    "所有查詢都判斷不出結果，網站版面可能改了。請用 probe 模式檢查截圖。",
                    color=0xE67E22,
                )
                state["_warned"] = "yes"
        else:
            state["_warned"] = "no"

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if state.get("_heartbeat") != month:
            notify("💤 空房監控運作中",
                   f"{month} 月報：{total - len(unknowns)}/{total} 筆查詢正常判讀。",
                   color=0x95A5A6)
            state["_heartbeat"] = month

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
