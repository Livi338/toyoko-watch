#!/usr/bin/env python3
"""
東橫INN 空房監控（非會員、不需帳密）

監控的行程來自 GitHub Issue：
  - 用「🏨 新增空房監控行程」表單開一張 Issue = 新增一趟行程
  - 編輯 Issue = 修改設定（狀態會重新計算）
  - 關閉 Issue = 停止監控；行程日期過了會自動關閉
只讀取 repo 擁有者本人開的 Issue，別人開的會被忽略。

用法：
    python check_niigata.py            # 正常執行
    python check_niigata.py --probe    # 除錯：存下每頁截圖與文字
"""

import hashlib
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
# 固定設定
# ─────────────────────────────────────────────────────────────

SMOKING = "noSmoking"   # 只查禁菸房

SEARCH_URL = (
    "https://www.toyoko-inn.com/china/search/result/room_plan/"
    "?hotel={hotel}&people={people}&room={room}&smoking={smoking}"
    "&start={start}&end={end}"
)
HOTEL_LIST_URL = "https://www.toyoko-inn.com/hotel_list/"

SOLD_OUT_TEXT = "沒有空房"
PRICE_RE = re.compile(r"(¥|￥|JPY)\s*[\d,]{3,}|[\d,]{3,}\s*(日圓|日元|円)")

# 表單「房型」選項 → (房型名稱必須包含, 房型名稱不能包含)
ROOM_FILTERS = {
    "單人房（含經濟單人房）": (["單人"], []),
    "單人房（不含經濟單人房）": (["單人"], ["經濟"]),
    "不限房型": ([], []),
}

CONFIG_VERSION = "2"     # 改了解析邏輯就加 1，所有 Issue 會重新確認並留言
MAX_QUERIES = 60        # 所有行程加起來的查詢上限，避免單次執行跑太久
STATE_FILE = Path("state_niigata.json")
TIMEOUT_MS = 45_000
JST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────────────────────────

WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
GH_TOKEN = os.environ.get("GITHUB_TOKEN")
REPO = os.environ.get("GITHUB_REPOSITORY")          # 例：Livi338/toyoko-watch
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER")   # 例：Livi338
PROBE = "--probe" in sys.argv


def log(msg):
    print(msg, flush=True)


# ─── Discord ─────────────────────────────────────────────────

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


# ─── GitHub Issue ────────────────────────────────────────────

def gh(method, path, **kw):
    r = requests.request(
        method, f"https://api.github.com/repos/{REPO}{path}",
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        timeout=20, **kw,
    )
    r.raise_for_status()
    return r.json() if r.text else None


def fetch_issues():
    if not (GH_TOKEN and REPO):
        log("⚠️  沒有 GITHUB_TOKEN / GITHUB_REPOSITORY，無法讀取 Issue")
        return []
    issues = gh("GET", "/issues", params={"state": "open", "per_page": 100})
    return [i for i in issues
            if "pull_request" not in i
            and i["user"]["login"] == OWNER
            and "### 旅遊名稱" in (i.get("body") or "")]


def comment(number, text):
    if PROBE:
        log(f"[probe] 不留言 #{number}: {text[:80]}")
        return
    try:
        gh("POST", f"/issues/{number}/comments", json={"body": text})
    except Exception as e:
        log(f"留言失敗 #{number}: {e}")


def close_issue(number):
    if PROBE:
        return
    try:
        gh("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": "completed"})
    except Exception as e:
        log(f"關閉失敗 #{number}: {e}")


def parse_form(body):
    """把 Issue 表單內容轉成 {欄位標題: 值}"""
    fields, parts = {}, re.split(r"^###\s+(.+?)\s*$", body, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        val = parts[i + 1].strip()
        fields[parts[i].strip()] = "" if val == "_No response_" else val
    return fields


# ─── 飯店名稱 → 代碼 ─────────────────────────────────────────

TRANS = str.maketrans({
    "橫": "横", "站": "駅", "驛": "駅", "濱": "浜", "瀉": "潟", "櫻": "桜",
    "廣": "広", "藏": "蔵", "澤": "沢", "邊": "辺", "國": "国", "關": "関",
    "帶": "帯", "條": "条", "號": "号", "團": "団", "實": "実", "濟": "済",
    "內": "内", "惠": "恵", "黑": "黒", "髙": "高", "峯": "峰", "鄉": "郷",
    "Ａ": "A", "Ｂ": "B", "１": "1", "２": "2", "３": "3", "４": "4",
})


def norm(s):
    s = s.translate(TRANS)
    s = re.sub(r"東横\s*INN|東横イン|TOYOKO\s*INN", "", s, flags=re.I)
    return re.sub(r"[\s・·()（）「」]", "", s).upper()


def load_hotel_list(browser, state):
    """回傳 {代碼: [日文名稱, 中文名稱, ...]}，兩種語言的寫法都能比對"""
    cache = state.get("_hotels", {})
    if (cache.get("date") == date.today().isoformat() and cache.get("v") == 2
            and cache.get("list")):
        return cache["list"]
    hotels = {}
    for locale, url in (("ja-JP", HOTEL_LIST_URL),
                        ("zh-TW", HOTEL_LIST_URL.replace(".com/", ".com/china/"))):
        try:
            ctx = browser.new_context(locale=locale)
            pg = ctx.new_page()
            pg.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
            pairs = pg.eval_on_selector_all(
                "a[href*='/search/detail/']",
                "els => els.map(e => [e.getAttribute('href'), e.innerText.trim()])",
            )
            ctx.close()
        except Exception as e:
            log(f"⚠️  飯店列表（{locale}）讀取失敗：{e}")
            continue
        for href, text in pairs:
            m = re.search(r"/search/detail/(\d{5})", href or "")
            if m and text:
                name = text.splitlines()[0].strip()
                names = hotels.setdefault(m.group(1), [])
                if name not in names:
                    names.append(name)
    state["_hotels"] = {"date": date.today().isoformat(), "v": 2, "list": hotels}
    log(f"飯店列表：共 {len(hotels)} 間")
    return hotels


def resolve_hotel(query, hotel_list):
    """回傳 (代碼, 正式名稱, 錯誤訊息)"""
    q = query.strip().lstrip("-*• ").strip()
    if not q:
        return None, None, None
    if re.fullmatch(r"\d{5}", q):
        if q in hotel_list:
            return q, hotel_list[q][0], None
        return None, None, f"找不到代碼 `{q}`"
    nq = norm(q)
    exact = [(c, ns[0]) for c, ns in hotel_list.items()
             if any(norm(n) == nq for n in ns)]
    if len(exact) == 1:
        return exact[0][0], exact[0][1], None
    part = [(c, ns[0]) for c, ns in hotel_list.items()
            if nq and any(nq in norm(n) for n in ns)]
    if len(part) == 1:
        return part[0][0], part[0][1], None
    if part:
        names = "、".join(f"{n}（`{c}`）" for c, n in part[:8])
        return None, None, f"「{q}」符合多間：{names}，請打完整一點或直接填代碼"
    return None, None, f"找不到「{q}」，請確認名稱或直接填 5 碼代碼"


# ─── 讀取 Issue 變成行程 ─────────────────────────────────────

def build_trip(issue, hotel_list, today):
    """回傳 (行程 dict 或 None, 錯誤清單)"""
    f = parse_form(issue["body"])
    errors = []

    name = f.get("旅遊名稱") or f"行程 #{issue['number']}"
    try:
        ci = date.fromisoformat(f.get("入住日", ""))
        co = date.fromisoformat(f.get("退房日", ""))
        if co <= ci:
            errors.append("退房日必須晚於入住日")
    except ValueError:
        errors.append("日期格式要是 YYYY-MM-DD，例如 2026-12-12")
        ci = co = None

    hotels, warnings = {}, []
    for line in (f.get("飯店（依志願排序）") or "").splitlines():
        cd, official, err = resolve_hotel(line, hotel_list)
        if cd or err:
            log(f"  #{issue['number']} 「{line.strip()}」→ {cd or '-'} {official or err}")
        if err:
            errors.append(err)
        elif cd in hotels:
            warnings.append(f"「{line.strip()}」對應到 {official}（`{cd}`），"
                            f"跟前面重複，已略過。如果不是這間，請直接填代碼")
        elif cd:
            hotels[cd] = official
    if not hotels and not errors:
        errors.append("至少要填一間飯店")

    roomtype = f.get("房型") or "單人房（含經濟單人房）"
    if roomtype not in ROOM_FILTERS:
        roomtype = "單人房（含經濟單人房）"

    if errors:
        return None, errors

    dates = [(ci + timedelta(days=i)).isoformat() for i in range((co - ci).days)]
    when = f.get("什麼時候通知") or ""
    min_streak = (len(dates) if "整段" in when else 2 if "2 晚" in when else 1)

    return {
        "number": issue["number"],
        "name": name,
        "hotels": hotels,
        "dates": dates,
        "people": int(f.get("總人數") or 1),
        "rooms": int(f.get("房間數") or 1),
        "roomtype": roomtype,
        "min_streak": min_streak,
        "checkout": co,
        "warnings": warnings,
    }, []


def describe(trip):
    lines = [f"✅ **開始監控：{trip['name']}**", ""]
    lines.append(f"- 日期：{trip['dates'][0]} 入住，{trip['checkout']} 退房（{len(trip['dates'])} 晚）")
    lines.append(f"- 人數 / 房間：{trip['people']} 人 / {trip['rooms']} 間")
    lines.append(f"- 房型：{trip['roomtype']}（禁菸）")
    when = {1: "任何一晚有房就通知", len(trip["dates"]): "整段都能同一間才通知"}.get(
        trip["min_streak"], f"至少能連住 {trip['min_streak']} 晚才通知")
    lines.append(f"- 通知條件：{when}")
    lines.append("- 飯店：")
    for rank, (cd, n) in enumerate(trip["hotels"].items()):
        label = "⭐第一志願" if rank == 0 else f"備案{rank}"
        lines.append(f"  {rank + 1}. {n}（`{cd}`，{label}）")
    if trip.get("warnings"):
        lines += ["", "⚠️ **請確認：**"] + [f"- {w}" for w in trip["warnings"]]
    lines += ["", "有房時會發 Discord 通知。要修改請直接編輯這張 Issue，要停止請關閉它。"]
    return "\n".join(lines)


# ─── 頁面判讀 ────────────────────────────────────────────────

def build_url(hotel_cd, start, end, people, rooms):
    return SEARCH_URL.format(hotel=hotel_cd, people=people, room=rooms,
                             smoking=SMOKING, start=start, end=end)


def room_types(text):
    found = []
    for line in text.splitlines():
        s = line.strip()
        if (2 <= len(s) <= 12 and s.endswith("房") and "空房" not in s
                and not re.search(r"[，。、：；]", s) and s not in found):
            found.append(s)
    return found


def classify(text, include, exclude):
    """回傳 (available / soldout / unknown, 有房的目標房型清單)"""
    if not include and not exclude:
        if PRICE_RE.search(text):
            return "available", []
        if SOLD_OUT_TEXT in text:
            return "soldout", []
        return "unknown", []

    targets = [t for t in room_types(text)
               if all(k in t for k in include) and not any(k in t for k in exclude)]
    if not targets:
        # 找不到任何目標房型：頁面上有其他房型標題 → 代表這間沒有該房型；否則版面可能變了
        return ("soldout", []) if room_types(text) else ("unknown", [])
    lines = [s.strip() for s in text.splitlines()]
    open_types = [t for t in targets
                  if not any(s.startswith(f"{t}{SOLD_OUT_TEXT}") for s in lines)]
    if open_types and PRICE_RE.search(text):
        return "available", open_types
    if not open_types:
        return "soldout", []
    return "unknown", []


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
    return text


# ─── 通知整理 ────────────────────────────────────────────────

def streaks(avail_dates, trip_dates):
    runs, cur = [], []
    for d in sorted(trip_dates):
        if d in avail_dates:
            cur.append(d)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [(r[0], r[-1]) for r in runs]


def nights(run):
    return (date.fromisoformat(run[1]) - date.fromisoformat(run[0])).days + 1


def md(d):
    x = date.fromisoformat(d)
    return f"{x.month}/{x.day}"


def summarize(trip, avail, today, types_map, prefix):
    dates = [d for d in trip["dates"] if date.fromisoformat(d) >= today]
    full, multi, single = [], [], []
    best, first_choice_full = 0, False
    for rank, (cd, name) in enumerate(trip["hotels"].items()):
        label = "⭐第一志願" if rank == 0 else f"備案{rank}"
        for run in streaks(avail.get(cd, set()), dates):
            n = nights(run)
            best = max(best, n)
            end = (date.fromisoformat(run[1]) + timedelta(days=1)).isoformat()
            url = build_url(cd, run[0], end, trip["people"], trip["rooms"])
            span = md(run[0]) if n == 1 else f"{md(run[0])}–{md(run[1])}"
            kinds = []
            for d in dates:
                if run[0] <= d <= run[1]:
                    for t in types_map.get(f"{prefix}{cd}:{d}", []):
                        if t not in kinds:
                            kinds.append(t)
            kind_txt = f"\n　房型：{'、'.join(kinds)}" if kinds else ""
            line = (f"**{re.sub(r'^東横INN|^東橫INN', '', name)}**（{label}）"
                    f" {span}（{n} 晚）{kind_txt}\n{url}")
            item = (-n, rank, line)
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
        parts.append(f"⛔ 目前 {len(trip['hotels'])} 間都沒房的晚上：" + "、".join(gaps))

    if first_choice_full:
        title = f"🟢⭐ {trip['name']}：第一志願可一次訂齊 {len(dates)} 晚！"
    elif full:
        title = f"🟢 {trip['name']}：有飯店可一次訂齊 {len(dates)} 晚！"
    elif best > 1:
        title = f"🟡 {trip['name']}：最多可連住 {best} 晚"
    else:
        title = f"⚪ {trip['name']}：只有零散單晚"
    return title, "\n\n".join(parts), best


# ─── 主程式 ──────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def main():
    if not WEBHOOK and not PROBE:
        sys.exit("錯誤：請設定環境變數 DISCORD_WEBHOOK")

    state = load_state()
    hashes = state.setdefault("_issue_hash", {})
    types_map = state.setdefault("_types", {})
    today = datetime.now(JST).date()
    issues = fetch_issues()
    log(f"讀到 {len(issues)} 張監控 Issue")

    changed, unknowns, total = set(), [], 0

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

        hotel_list = load_hotel_list(browser, state) if issues else {}
        trips = []

        for issue in issues:
            num = str(issue["number"])
            h = hashlib.sha1((CONFIG_VERSION + issue["body"]).encode()).hexdigest()
            trip, errors = build_trip(issue, hotel_list, today)
            is_new = hashes.get(num) != h

            if is_new:
                # 設定變了：清掉這趟的舊狀態，重新計算
                for k in [k for k in state if k.startswith(f"{num}:")]:
                    del state[k]
                for k in [k for k in types_map if k.startswith(f"{num}:")]:
                    del types_map[k]
                hashes[num] = h
                if errors:
                    comment(issue["number"], "❌ **設定有問題，還沒開始監控：**\n\n"
                            + "\n".join(f"- {e}" for e in errors)
                            + "\n\n請編輯這張 Issue 修正，存檔後會自動重新檢查。")
                else:
                    comment(issue["number"], describe(trip))

            if errors:
                log(f"#{num} 設定錯誤：{errors}")
                continue
            if trip["checkout"] <= today:
                comment(issue["number"], "🏁 行程日期已過，自動結束監控。")
                close_issue(issue["number"])
                continue
            trips.append(trip)

        for trip in trips:
            include, exclude = ROOM_FILTERS[trip["roomtype"]]
            prefix = f"{trip['number']}:"
            for cd, name in trip["hotels"].items():
                for checkin in trip["dates"]:
                    if date.fromisoformat(checkin) < today:
                        continue
                    if total >= MAX_QUERIES:
                        log(f"⚠️  已達查詢上限 {MAX_QUERIES} 筆，其餘下次再查")
                        break
                    total += 1
                    key = f"{prefix}{cd}:{checkin}"
                    end = (date.fromisoformat(checkin) + timedelta(days=1)).isoformat()
                    url = build_url(cd, checkin, end, trip["people"], trip["rooms"])
                    try:
                        text = check_one(page, url, f"{trip['number']}_{cd}_{checkin}")
                        status, open_types = classify(text, include, exclude)
                    except Exception as e:
                        log(f"❌ {name} {checkin}: {e}")
                        status, open_types = "unknown", []

                    extra = f"（{'、'.join(open_types)}）" if open_types else ""
                    log(f"[{trip['name']}] {name} {checkin} 入住: {status}{extra}")

                    if status == "available" and state.get(key) != "available":
                        changed.add(trip["number"])
                    if status == "unknown":
                        unknowns.append(key)
                    else:
                        state[key] = status
                        types_map[key] = open_types

                    time.sleep(random.uniform(3, 6))  # 禮貌性間隔，不要拿掉

        browser.close()

    if not PROBE:
        for trip in trips:
            if trip["number"] not in changed:
                continue
            prefix = f"{trip['number']}:"
            avail = {}
            for cd in trip["hotels"]:
                ok = {d for d in trip["dates"] if state.get(f"{prefix}{cd}:{d}") == "available"}
                if ok:
                    avail[cd] = ok
            title, body, best = summarize(trip, avail, today, types_map, prefix)
            if best >= trip["min_streak"]:
                color = 0x2ECC71 if title.startswith("🟢") else (
                    0xF1C40F if title.startswith("🟡") else 0x95A5A6)
                notify(title, body, color=color)

        if total and len(unknowns) == total:
            if state.get("_warned") != "yes":
                notify("⚠️ 空房監控可能失效",
                       "所有查詢都判斷不出結果，網站版面可能改了。請用 probe 模式檢查截圖。",
                       color=0xE67E22)
                state["_warned"] = "yes"
        else:
            state["_warned"] = "no"

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if state.get("_heartbeat") != month:
            notify("💤 空房監控運作中",
                   f"{month} 月報：{len(trips)} 趟行程，"
                   f"{total - len(unknowns)}/{total} 筆查詢正常判讀。",
                   color=0x95A5A6)
            state["_heartbeat"] = month

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
