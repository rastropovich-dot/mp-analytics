#!/usr/bin/env python3
"""Доставка книги «Ozon - <месяц>» в Telegram: собрать генератором из базы и отправить файлом (sendDocument).

    venv/bin/python3 scripts/send_ozon_month_report.py --no-send     сухой прогон: книга собирается, в Telegram не уходит ничего
    venv/bin/python3 scripts/send_ozon_month_report.py               собрать и отправить тем же ботом, что и утренний алерт

Какой месяц. Лист кончается вчерашним днём (сегодняшний ночь ещё не грузила), поэтому месяц — тот, в котором
ВЧЕРА. 1-го числа вчера — последний день прошлого месяца: уходит прошлый месяц целиком, а текущего ещё нет ни дня.
Отдельного правила для первого числа поэтому нет — оно получается само. --month / --date-to задают окно руками.

Источник — база плюс живой ответ by-day за дни моложе двух суток (`--fetch young`, 1–3 обращения на день): реклама за D−1
доезжает после ночного сбора, и в леджере её ещё нет. Живого ответа нет — день остаётся на леджере с примечанием
«реклама не доехала». Файлов сырья на Render нет и не нужно. Дня нет в леджере — пустые реклама и эквайринг с примечанием.

Книга сохраняется в data/reports/ozon_<месяц>_to_<дата>.xlsx (gitignored) — та же, что ушла в Telegram; путь печатается.
На Render диск cron-задачи эфемерный: файл живёт до конца прогона. Поэтому после sendDocument в лог уходит одна строка
«книга в Telegram: <имя> file_id=… file_unique_id=… size=… sha256=…» (и то же — в спутник <книга>.json, раздел delivery):
scripts/fetch_ozon_month_report.py находит её в логе Render и скачивает ту же книгу на машину владельца, сверяя sha256.

Шаг нефатальный для утреннего алерта: alerts_telegram.py зовёт его отдельным процессом уже ПОСЛЕ отправки
сообщения. Коды: 0 — книга ушла (или собрана при --no-send); 1 — не вышло, и об этом уже сказано в Telegram
одной строкой (при --no-send — в выводе); любой другой — процесс умер раньше, чем успел сказать.
В БД не пишет: db_writes = 0.
"""
import argparse
import calendar
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

GENERATOR = os.path.join(ROOT, "scripts", "report_ozon_month.py")
REPORTS_DIR = os.path.join(ROOT, "data", "reports")
GENERATOR_TIMEOUT = 900            # сентябрь собирается ~60 с; запас — на медленную базу, а не на зависание
ORDERS_SHEET_FAILED = 2            # код генератора: книга записана, но лист «Заказы» не собран
MONTHS_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def yesterday_local(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))).date() - timedelta(days=1)


def window_for(yesterday):
    """(месяц YYYY-MM, последний день листа): месяц вчерашнего дня, по вчера."""
    return yesterday.strftime("%Y-%m"), yesterday.isoformat()


def month_end(month):
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, calendar.monthrange(y, m)[1]).isoformat()


def mln(value):
    return "—" if value is None else f"{float(value) / 1_000_000:,.1f} млн".replace(",", " ").replace(".", ",")


def pct(value):
    return "—" if value is None else f"{float(value) * 100:.1f} %".replace(".", ",")


def build_caption(month, date_to, summary, orders_failed):
    last = date.fromisoformat(date_to)
    lines = [f"Ozon — {MONTHS[last.month - 1]} {last.year}, по {last.day} {MONTHS_GENITIVE[last.month - 1]}"]
    b, o = (summary or {}).get("buyouts") or {}, (summary or {}).get("orders") or {}
    if b:
        lines.append(f"Выкупы: оборот {mln(b.get('turnover'))}, выручка {mln(b.get('revenue'))}, фин. рез. {mln(b.get('fin_result'))}, Ebitda {mln(b.get('ebitda'))}"
                     + (f", фин. рез. по индексу СС {mln(b.get('fin_result_index'))}" if b.get("fin_result_index") is not None else ""))
    if o:
        lines.append(f"Заказы: создано {mln(o.get('created'))}, прогноз подтв. {mln(o.get('forecast_confirmed'))}, "
                     f"ДРР {pct(o.get('drr_created'))} от созданного / {pct(o.get('drr_forecast'))} от прогноза, фин. рез. прогноз {mln(o.get('fin_result'))}")
        lines.append(f"Кривая дозревания: ночи {o.get('curve_nights')}; дней в прогнозе {o.get('forecast_days')}, дозревших {o.get('mature_days')}")
    if orders_failed:
        lines.append("⚠️ Лист «Заказы» не собран — причина на самом листе; остальные листы на месте.")
    if (summary or {}).get("warnings"):
        lines.append("⚠️ " + "; ".join(summary["warnings"]))
    return "\n".join(lines)[:1024]


def generate(month, date_to, out_dir):
    """Зовёт генератор отдельным процессом. Возвращает (путь | None, сводка | None, код генератора, хвост вывода)."""
    out = os.path.join(out_dir, f"ozon_{month}_to_{date_to}.xlsx")
    summary_path = out + ".json"
    cmd = [sys.executable, GENERATOR, "--month", month, "--date-to", date_to, "--fetch", "young", "--out", out, "--summary-json", summary_path]
    res = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=GENERATOR_TIMEOUT)
    text = "\n".join(line for line in (res.stdout or "").splitlines() if "Warning" not in line and "warnings.warn" not in line)
    print(text)
    ok = res.returncode in (0, ORDERS_SHEET_FAILED) and os.path.exists(out)
    summary = json.load(open(summary_path)) if ok and os.path.exists(summary_path) else None
    return (out if ok else None), summary, res.returncode, text[-600:]


def telegram_call(method, token, **kwargs):
    """Вызов Bot API. Адрес с токеном не печатается. Возвращает (успех, что ответили, тело ответа)."""
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=120, **kwargs)
    try:
        body = resp.json()
    except ValueError:
        body = {"ok": False, "description": resp.text[:200]}
    said = f"HTTP {resp.status_code}, ok={body.get('ok')}" + (f", {body.get('description')}" if body.get("description") else "")
    return bool(resp.status_code == 200 and body.get("ok")), said, body


def telegram(method, token, **kwargs):
    ok, said, _body = telegram_call(method, token, **kwargs)
    return ok, said


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sent_document(body):
    """Из ответа sendDocument — то, по чему книгу можно скачать обратно: file_id, file_unique_id, размер, имя."""
    doc = ((body or {}).get("result") or {}).get("document") or {}
    return {k: doc.get(k) for k in ("file_id", "file_unique_id", "file_size", "file_name")}


def delivery_line(name, doc, size, sha):
    """Одна строка лога — её ищет fetch_ozon_month_report.py. Формат не менять без правки парсера там."""
    return (f"книга в Telegram: {name} file_id={doc.get('file_id')} file_unique_id={doc.get('file_unique_id')} "
            f"size={size} sha256={sha}")


def send_document(path, caption, token, chat_id):
    """(успех, что ответили, {file_id, file_unique_id, file_size, file_name})."""
    with open(path, "rb") as fh:
        ok, said, body = telegram_call("sendDocument", token, data={"chat_id": chat_id, "caption": caption},
                                       files={"document": (os.path.basename(path), fh, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    return ok, said, sent_document(body)


def record_delivery(path, doc, size, sha):
    """Раздел delivery в спутник <книга>.json (тот же файл, что пишет генератор сводкой). Отказ — не повод ронять доставку."""
    side = path + ".json"
    try:
        data = json.load(open(side)) if os.path.exists(side) else {}
        data["delivery"] = {**doc, "size": size, "sha256": sha, "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        json.dump(data, open(side, "w"), ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001
        print(f"  спутник {side} не дописан: {type(exc).__name__}: {exc}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Собрать книгу «Ozon - <месяц>» из базы и отправить в Telegram.")
    ap.add_argument("--no-send", action="store_true", help="собрать книгу и показать подпись, в Telegram ничего не отправлять")
    ap.add_argument("--month", help="YYYY-MM; по умолчанию — месяц вчерашнего дня")
    ap.add_argument("--date-to", help="последний день листа; по умолчанию — вчера по местному времени")
    ap.add_argument("--out-dir", help=f"куда положить книгу; по умолчанию — {REPORTS_DIR}")
    args = ap.parse_args(argv)
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not args.no_send and not (token and chat_id):
        print("лист Ozon: не заполнены TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — отправлять нечем")
        return 1
    default_month, default_to = window_for(yesterday_local())
    month = args.month or default_month
    date_to = args.date_to or (default_to if month == default_month else month_end(month))   # чужой месяц без --date-to — целиком
    out_dir = args.out_dir or REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    print(f"лист Ozon: месяц {month}, по {date_to}; отправка {'ВЫКЛЮЧЕНА (--no-send)' if args.no_send else 'включена'}")

    def fail(reason):
        print(f"лист Ozon НЕ отправлен: {reason}")
        if not args.no_send:
            ok, said = telegram("sendMessage", token, json={"chat_id": chat_id, "text": f"⚠️ Лист Ozon за {month} не отправлен: {reason}"[:1000]})
            print(f"  предупреждение в Telegram: {said}")
        return 1

    try:
        path, summary, code, tail = generate(month, date_to, out_dir)
    except subprocess.TimeoutExpired:
        return fail(f"генератор не уложился в {GENERATOR_TIMEOUT} с")
    except Exception as exc:  # noqa: BLE001
        return fail(f"{type(exc).__name__}: {exc}")
    if path is None:
        return fail(f"генератор завершился с кодом {code}: …{tail[-300:]}")
    caption = build_caption(month, date_to, summary, code == ORDERS_SHEET_FAILED)
    size = os.path.getsize(path)
    sha = sha256_of(path)
    print(f"\nкнига: {path}, {size:,} байт".replace(",", " ") + f", sha256 {sha}")
    print("подпись:\n" + caption)
    if args.no_send:
        print("\n--no-send: в Telegram не отправлено ничего. db_writes = 0")
        return 0
    try:
        ok, said, *rest = send_document(path, caption, token, chat_id)
    except Exception as exc:  # noqa: BLE001
        return fail(f"sendDocument: {type(exc).__name__}: {exc}")
    print(f"sendDocument: {said}")
    if not ok:
        return fail(f"sendDocument ответил: {said}")
    doc = rest[0] if rest else {}
    print(delivery_line(os.path.basename(path), doc, size, sha))
    if not doc.get("file_id"):
        print("  ⚠️ в ответе sendDocument нет file_id — скачать книгу обратно из Telegram будет нечем")
    record_delivery(path, doc, size, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
