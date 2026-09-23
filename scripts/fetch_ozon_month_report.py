#!/usr/bin/env python3
"""Скачать утреннюю книгу «Ozon - <месяц>» из Telegram на машину владельца и сверить её хэш.

    venv/bin/python3 scripts/fetch_ozon_month_report.py --date-to 2026-09-23          книга по 23-е, ушедшая утром 24-го
    venv/bin/python3 scripts/fetch_ozon_month_report.py --file-id <id> [--sha256 <hex>] [--name ozon_2026-09_to_2026-09-23.xlsx]

Зачем. Книгу собирает и отправляет cron-задача Render mp-analytics-telegram-report; её диск эфемерный — файл
data/reports/… живёт до конца прогона, и открыть его с машины владельца нельзя (09-23 книгу сверяли по логу).
Доставка печатает в лог строку «книга в Telegram: <имя> file_id=… file_unique_id=… size=… sha256=…».

--date-to D: строка ищется в логе этой задачи через API Render (RENDER_API_KEY в .env) за сутки D+1 и D+2 UTC —
утренняя доставка идёт на следующий день после D; берётся последняя строка с именем ozon_<месяц D>_to_<D>.xlsx.
Дальше getFile → скачивание по file_path тем же ботом (TELEGRAM_BOT_TOKEN), sha256 и размер скачанного сверяются
с записанными при отправке. Не совпало — файл не сохраняется, код 1: скачано не то, что ушло.

Кладёт в data/reports/<имя> (gitignored) и спутник <имя>.download.json (file_id, sha256, размер, откуда, когда);
печатает путь, размер и sha256. В БД не пишет; обращений: Render 1–3, Telegram 2.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

REPORTS_DIR = os.path.join(ROOT, "data", "reports")
RENDER_OWNER = "tea-d7n5qs1f9bms738bfvug"
RENDER_ALERT_SERVICE = "crn-d7t5ed1j2pic73aiqmog"      # mp-analytics-telegram-report, 30 7 * * *
LINE_RE = re.compile(r"книга в Telegram: (?P<name>\S+) file_id=(?P<file_id>\S+) file_unique_id=(?P<file_unique_id>\S+) "
                     r"size=(?P<size>\d+) sha256=(?P<sha256>[0-9a-f]{64})")


def parse_delivery_line(text):
    """Строка лога доставки → {name, file_id, file_unique_id, size, sha256} или None."""
    m = LINE_RE.search(text or "")
    if not m:
        return None
    out = m.groupdict()
    out["size"] = int(out["size"])
    return out


def book_name(date_to):
    return f"ozon_{date_to[:7]}_to_{date_to}.xlsx"


def pick_delivery(lines, name):
    """Последняя по времени строка доставки нужной книги. lines — [(timestamp, message)]."""
    found = [(ts, parse_delivery_line(msg)) for ts, msg in lines]
    found = [(ts, d) for ts, d in found if d and d["name"] == name]
    return max(found, key=lambda x: x[0])[1] if found else None


def render_log_lines(date_to, api_key, get=None):
    """Строки лога утренней задачи с «книга в Telegram» за D+1 … D+2 UTC. Возвращает ([(ts, msg)], обращений)."""
    get = get or requests.get
    start = datetime.combine(date.fromisoformat(date_to) + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=2)
    params = {"ownerId": RENDER_OWNER, "resource": RENDER_ALERT_SERVICE, "text": "книга в Telegram",
              "startTime": start.isoformat().replace("+00:00", "Z"), "endTime": end.isoformat().replace("+00:00", "Z"),
              "direction": "forward", "limit": 100}
    lines, calls = [], 0
    while True:
        resp = get(f"https://api.render.com/v1/logs?{urllib.parse.urlencode(params)}",
                   headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}, timeout=60)
        calls += 1
        if resp.status_code != 200:
            raise RuntimeError(f"Render logs: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        lines.extend((row.get("timestamp", ""), row.get("message", "")) for row in data.get("logs") or [])
        if not data.get("hasMore") or calls >= 10:
            return lines, calls
        params["startTime"], params["endTime"] = data["nextStartTime"], data["nextEndTime"]


def telegram_download(file_id, token, get=None):
    """getFile → байты файла. Адрес с токеном не печатается. Возвращает (байты, file_path, file_size по Telegram)."""
    get = get or requests.get
    resp = get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=60)
    body = resp.json() if resp.status_code == 200 else {}
    if not body.get("ok"):
        raise RuntimeError(f"getFile: HTTP {resp.status_code}, {str(body.get('description') or resp.text)[:200]}")
    file_path, tg_size = body["result"]["file_path"], body["result"].get("file_size")
    data = get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=120)
    if data.status_code != 200:
        raise RuntimeError(f"скачивание файла: HTTP {data.status_code}")
    return data.content, file_path, tg_size


def check(content, expected_sha, expected_size):
    """(sha256 скачанного, список расхождений). Пусто — совпало всё, что было с чем сравнить."""
    sha = hashlib.sha256(content).hexdigest()
    problems = []
    if expected_sha and sha != expected_sha:
        problems.append(f"sha256 скачанного {sha} ≠ отправленного {expected_sha}")
    if expected_size is not None and len(content) != int(expected_size):
        problems.append(f"размер скачанного {len(content)} ≠ отправленного {expected_size}")
    return sha, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Скачать книгу Ozon из Telegram и сверить хэш.")
    ap.add_argument("--date-to", help="последний день книги (YYYY-MM-DD) — строка доставки ищется в логе Render")
    ap.add_argument("--file-id", help="file_id из строки доставки — без обращения к Render")
    ap.add_argument("--sha256", help="с --file-id: ожидаемый sha256")
    ap.add_argument("--name", help="с --file-id: имя файла; по умолчанию — по --date-to или file_id")
    ap.add_argument("--out-dir", default=REPORTS_DIR)
    args = ap.parse_args(argv)
    if not (args.date_to or args.file_id):
        ap.error("нужен --date-to или --file-id")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("не заполнен TELEGRAM_BOT_TOKEN — скачивать нечем")
        return 1

    if args.file_id:
        delivery = {"file_id": args.file_id, "sha256": args.sha256, "size": None, "file_unique_id": None,
                    "name": args.name or (book_name(args.date_to) if args.date_to else f"ozon_book_{args.file_id[-12:]}.xlsx")}
        source = "--file-id"
    else:
        api_key = os.getenv("RENDER_API_KEY")
        if not api_key:
            print("не заполнен RENDER_API_KEY — строку доставки искать негде; дайте --file-id")
            return 1
        name = book_name(args.date_to)
        lines, calls = render_log_lines(args.date_to, api_key)
        delivery = pick_delivery(lines, name)
        print(f"лог Render ({RENDER_ALERT_SERVICE}): строк доставки {len(lines)}, обращений {calls}")
        if not delivery:
            print(f"строки «книга в Telegram: {name} …» в логе нет — книга не уходила или ушла до того, как доставка "
                  "начала печатать file_id; скачать нечего")
            return 1
        source = "лог Render"

    content, file_path, tg_size = telegram_download(delivery["file_id"], token)
    sha, problems = check(content, delivery.get("sha256"), delivery.get("size"))
    if problems:
        print("НЕ СОВПАЛО, файл не сохранён: " + "; ".join(problems))
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, delivery["name"])
    with open(path, "wb") as fh:
        fh.write(content)
    json.dump({"file_id": delivery["file_id"], "file_unique_id": delivery.get("file_unique_id"), "telegram_file_path": file_path,
               "telegram_file_size": tg_size, "size": len(content), "sha256": sha, "sha256_sent": delivery.get("sha256"),
               "source": source, "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
              open(path + ".download.json", "w"), ensure_ascii=False, indent=1)
    verdict = "совпал с отправленным" if delivery.get("sha256") else "сверять не с чем (--file-id без --sha256)"
    print(f"книга: {path}, {len(content):,} байт".replace(",", " ") + f", sha256 {sha} — {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
