#!/usr/bin/env python3
"""Снять логи Render за окно времени в файл — страницы с перекрытием и дедупликацией по id.

Урок 2026-09-27: постраничный сбор «следующая страница с последней секунды» пропускает строки той же секунды на
границе страницы (пропали 7 строк финиша ночи). Здесь следующая страница начинается на OVERLAP_S секунд раньше
последней полученной строки, повторы отбрасываются по id, стоп — когда страница не принесла ни одной новой строки.
Между обращениями пауза PAUSE_S, 429 Render API — ждать Retry-After (или 5 с) и повторить, не более 5 раз.

Пример: python3 scripts/fetch_render_logs.py --resource crn-d7n7nan7f7vs73fk70kg \
            --start 2026-09-28T00:00:00Z --end 2026-09-28T09:00:00Z --out logs/render_20260928/night
Пишет <out>.jsonl (сырые записи) и <out>.txt (HH:MM:SS + текст без ANSI). Только чтение, к базе не ходит.
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cabinet  # noqa: E402  — организация Render из профиля; ключ — из окружения или .env СВОЕГО каталога (чужой .env не читается)

OWNER = cabinet.profile().RENDER["owner"]
PAUSE_S = 1.2
OVERLAP_S = 2
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def load_key():
    key = os.environ.get("RENDER_API_KEY")
    for p in (os.path.join(ROOT, ".env"),):
        if key:
            break
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith("RENDER_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        sys.exit("RENDER_API_KEY не найден")
    return key


def get(url, key, stats):
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                stats["calls"] += 1
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            stats["calls"] += 1
            if exc.code == 429 and attempt < 5:
                stats["429"] += 1
                wait = float(exc.headers.get("Retry-After") or 5)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Render API: 429 шесть раз подряд")


def parse_ts(s):
    """Render отдаёт наносекунды (9 знаков) — fromisoformat берёт не больше 6."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$", s)
    if not m:
        raise ValueError(f"неожиданный формат времени Render: {s!r}")
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}")


def bump_ns(ts):
    """То же время + 1 нс (строкой, без потери точности) — когда страница целиком внутри одного момента."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$", ts)
    if not m:
        raise ValueError(f"неожиданный формат времени Render: {ts!r}")
    frac = (m.group(2) or "0").ljust(9, "0")[:9]
    n = int(frac) + 1
    sec = m.group(1)
    if n >= 10 ** 9:
        n = 0
        sec = (datetime.fromisoformat(sec) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{sec}.{n:09d}{m.group(3)}"


def fetch(resource, start, end, key, limit=100):
    """Курсор — точное (наносекундное) время последней полученной строки, как его отдал Render: строки того же
    момента приходят повторно и отсеиваются по id; страница без единой новой строки при полном размере — курсор
    на 1 нс вперёд. Так граница страницы не теряет строки одной секунды (урок 2026-09-27)."""
    stats = {"calls": 0, "429": 0, "pages": 0, "dupes": 0}
    seen, rows = set(), []
    cursor = start
    while True:
        q = urllib.parse.urlencode({"ownerId": OWNER, "resource": resource, "startTime": cursor, "endTime": end,
                                    "limit": limit, "direction": "forward"})
        d = get("https://api.render.com/v1/logs?" + q, key, stats)
        stats["pages"] += 1
        page = d.get("logs", []) if isinstance(d, dict) else d
        new = 0
        for e in page:
            if e.get("id") in seen:
                stats["dupes"] += 1
                continue
            seen.add(e.get("id"))
            rows.append(e)
            new += 1
        if not page:
            break
        has_more = bool(isinstance(d, dict) and d.get("hasMore"))
        if len(page) < limit and not has_more:
            break
        last_raw = page[-1]["timestamp"]
        cursor = bump_ns(last_raw) if new == 0 else last_raw
        time.sleep(PAUSE_S)
    rows.sort(key=lambda e: (parse_ts(e.get("timestamp", "1970-01-01T00:00:00Z")), e.get("timestamp", "")))
    return rows, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resource", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True, help="префикс файлов: <out>.jsonl и <out>.txt")
    a = ap.parse_args()
    key = load_key()
    rows, stats = fetch(a.resource, a.start, a.end, key)
    with open(a.out + ".jsonl", "w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with open(a.out + ".txt", "w", encoding="utf-8") as f:
        for e in rows:
            f.write(e.get("timestamp", "")[11:19] + " " + ANSI.sub("", e.get("message", "")) + "\n")
    print(f"{a.out}: строк {len(rows)}, страниц {stats['pages']}, обращений {stats['calls']}, 429 — {stats['429']}, повторов отброшено {stats['dupes']}")


if __name__ == "__main__":
    main()
