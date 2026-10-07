#!/usr/bin/env python3
"""Курсы золота / серебра / доллара / тенге / лома с картинок бота ЦУП (чат «LL Курсы», QompaX) → таблица (сорок седьмая §1).

    venv/bin/python3 scripts/ll_rates_ocr.py --export "~/Downloads/Telegram Desktop/ChatExport_2026-10-06"            разбор всех картинок
    venv/bin/python3 scripts/ll_rates_ocr.py --export … --sample 20 --seed 47                                           список картинок для ручной сверки

Что на картинке (формат один с 03.06.2023, меняется только размер — 1097×1954 до 27.10.2025, 1046×1863 после): шапка «Актуально на
<дата время>», в строке заголовка золота — курс доллара $ и фиксинг золота в $; пять блоков (золото 585, серебро 925, доллар, тенге,
цена скупки лома), в каждом — текущий курс с датой, курс 1С с датой установки, отклонение и отклонение в %.

OCR. tesseract на машине нет (задача говорила иначе — проверено `which tesseract`, 2026-10-06); распознаёт системный Vision macOS
(VNRecognizeTextRequest, ru+en) через два вспомогательных бинарника на Swift, исходники лежат в этом файле и компилируются один раз в
`data/ll_rates/bin/` (`swiftc` из Xcode CLT). Первый проход — вся картинка; поле не нашлось или не сошлась формула — второй проход
кропом зоны поля (увеличение ×2,5, затем ×4). Отклонение, % без второго знака («0,9%» вместо «0,96») восстанавливается из формулы и
помечается в колонке `derived`.

Приёмка (печатается): (а) формулы на каждой строке — отклонение = текущий − 1С, отклонение % = отклонение / 1С × 100 (допуск 0,011);
(б) `--sample N` — N случайных картинок для сверки глазами; (в) полнота — дней с картинкой по месяцам против result.json. Картинки не
бота (ширина < 600, скриншоты людей) не разбираются и перечисляются.

Выход (вне git, через cabinet.data_path): data/ll_rates/ll_rates.csv — строка на картинку; data/ll_rates/ll_rates_1c_history.csv —
история курса 1С (дата установки, значение, с какой картинки виден) по трём курсам; сырьё OCR — data/ll_rates/ocr/*.jsonl.
Только чтение; db_writes = 0; ни одного обращения к API.
"""
import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cabinet  # noqa: E402

_PROFILE = cabinet.profile()      # каталог data/ кабинета; базы здесь нет — guard assert_env не нужен, клиент не создаётся
OUT_DIR = cabinet.data_path("ll_rates", prof=_PROFILE)

D = Decimal
MON = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
BLOCKS = (("gold", "золота 585"), ("silver", "серебра 925"), ("usd", "курс доллара"), ("kzt", "курс тенге"), ("scrap", "скупки лома"))
FIELDS = ("date_cur", "date_1c", "cur", "c1", "dev", "devp")
NUM_RE = re.compile(r"^-?\d[\d ]*[,]\d{2}$")
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
TRANS = str.maketrans({"З": "3", "О": "0", "о": "0", "з": "3", "І": "1", "l": "1", "I": "1", "|": "1", "B": "8", "b": "6"})
MIN_BOT_WIDTH = 600
# относительное положение заголовков блоков и колонок — одинаково для обеих геометрий картинки
DEFAULT_TITLES = {"gold": 0.097, "silver": 0.226, "usd": 0.354, "kzt": 0.482, "scrap": 0.611}
COLS = {"date_cur": (0.16, 0.33), "date_1c": (0.46, 0.62), "cur": (0.02, 0.31), "c1": (0.33, 0.60), "dev": (0.62, 0.80), "devp": (0.80, 0.98)}
ROWS = {"date_cur": (0.027, 0.052), "date_1c": (0.027, 0.052), "cur": (0.055, 0.105), "c1": (0.055, 0.105), "dev": (0.055, 0.105), "devp": (0.055, 0.105)}
TOL = D("0.011")

SWIFT_FULL = r'''
import Foundation
import Vision
import AppKit
let args = CommandLine.arguments
guard args.count >= 3 else { fputs("usage: vision_ocr out.jsonl img...\n", stderr); exit(2) }
FileManager.default.createFile(atPath: args[1], contents: nil)
guard let out = FileHandle(forWritingAtPath: args[1]) else { exit(3) }
for path in args.dropFirst(2) {
    guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fputs("cannot read \(path)\n", stderr); continue }
    let W = cg.width, H = cg.height
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["ru-RU", "en-US"]
    do { try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req]) } catch { fputs("vision failed \(path): \(error)\n", stderr); continue }
    var lines: [[String: Any]] = []
    for obs in req.results ?? [] {
        guard let cand = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        lines.append(["text": cand.string, "x": (b.origin.x * Double(W) * 10).rounded() / 10, "y": ((1 - b.origin.y - b.height) * Double(H) * 10).rounded() / 10,
                      "w": (b.width * Double(W) * 10).rounded() / 10, "h": (b.height * Double(H) * 10).rounded() / 10, "conf": Double(cand.confidence)])
    }
    let rec: [String: Any] = ["file": path, "w": W, "h": H, "lines": lines]
    out.write(try! JSONSerialization.data(withJSONObject: rec, options: [])); out.write("\n".data(using: .utf8)!)
}
out.closeFile()
'''

SWIFT_CROP = r'''
import Foundation
import Vision
import AppKit
import CoreGraphics
let args = CommandLine.arguments
guard args.count >= 8, (args.count - 3) % 5 == 0 else { fputs("usage: vision_crop out.jsonl scale img x y w h ...\n", stderr); exit(2) }
let scale = Double(args[2]) ?? 2.0
FileManager.default.createFile(atPath: args[1], contents: nil)
guard let out = FileHandle(forWritingAtPath: args[1]) else { exit(3) }
var i = 3
while i + 4 < args.count {
    let path = args[i]; let cx = Int(args[i+1])!, cy = Int(args[i+2])!, cw = Int(args[i+3])!, ch = Int(args[i+4])!
    i += 5
    guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fputs("cannot read \(path)\n", stderr); continue }
    let rect = CGRect(x: cx, y: cy, width: cw, height: ch).intersection(CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
    guard let crop = cg.cropping(to: rect) else { fputs("crop failed \(path)\n", stderr); continue }
    let sw = Int(Double(crop.width) * scale), sh = Int(Double(crop.height) * scale)
    guard let ctx = CGContext(data: nil, width: sw, height: sh, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { continue }
    ctx.interpolationQuality = .high
    ctx.draw(crop, in: CGRect(x: 0, y: 0, width: sw, height: sh))
    guard let big = ctx.makeImage() else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["ru-RU", "en-US"]
    do { try VNImageRequestHandler(cgImage: big, options: [:]).perform([req]) } catch { fputs("vision failed \(path): \(error)\n", stderr); continue }
    var lines: [[String: Any]] = []
    for obs in req.results ?? [] {
        guard let cand = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        lines.append(["text": cand.string, "x": ((rect.origin.x + b.origin.x * rect.width) * 10).rounded() / 10, "y": ((rect.origin.y + (1 - b.origin.y - b.height) * rect.height) * 10).rounded() / 10,
                      "w": (b.width * rect.width * 10).rounded() / 10, "h": (b.height * rect.height * 10).rounded() / 10, "conf": Double(cand.confidence)])
    }
    let rec: [String: Any] = ["file": path, "lines": lines]
    out.write(try! JSONSerialization.data(withJSONObject: rec, options: [])); out.write("\n".data(using: .utf8)!)
}
out.closeFile()
'''


def ensure_binaries(out_dir):
    """Собрать vision_ocr / vision_crop, если их нет или исходник изменился (по хэшу текста)."""
    import hashlib
    bin_dir = os.path.join(out_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    paths = {}
    for name, src in (("vision_ocr", SWIFT_FULL), ("vision_crop", SWIFT_CROP)):
        digest = hashlib.sha256(src.encode()).hexdigest()[:12]
        exe = os.path.join(bin_dir, f"{name}_{digest}")
        if not os.path.exists(exe):
            swift_path = exe + ".swift"
            with open(swift_path, "w") as fh:
                fh.write(src)
            print(f"компилирую {name} (swiftc, ~40 с) …", flush=True)
            subprocess.run(["swiftc", "-O", "-o", exe, swift_path], check=True)
        paths[name] = exe
    return paths


# ---------------------------------------------------------------- разбор

def norm_num(text):
    text = text.strip().translate(TRANS).replace("$", "").strip()
    text = re.sub(r"^-\s+", "-", text).replace(".", ",")
    return text


def to_dec(text):
    """Число вида «6 636,22» / «-1,07» / «86.00» → Decimal; иначе None. Одного знака после запятой («0,9%») недостаточно."""
    t = norm_num(text).replace("%", "").strip()
    if not NUM_RE.match(t):
        return None
    try:
        return D(t.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def fixing_value(text):
    """«$4 122» → 4122; «$1 948,91» → 1948.91; «$2 941,3» (OCR потерял знак) → 2941.3; мусор → None."""
    t = norm_num(text).replace(" ", "")
    m = re.fullmatch(r"-?\d+(,\d{1,2})?", t)
    if not m:
        return None
    try:
        return D(t.replace(",", "."))
    except InvalidOperation:
        return None


def find_titles(lines, W, H):
    titles = {}
    for l in lines:
        tl = l["text"].lower()
        if "динамика" in tl:                     # заголовок графика «Динамика курса золота 585» — не блок
            continue
        for key, pat in BLOCKS:
            if pat in tl and (l["x"] + l["w"] / 2) / W < 0.45 and key not in titles:
                titles[key] = l["y"] / H
    return titles


def parse_full(rec):
    """Первый проход по строкам OCR всей картинки → поля; чего нет — None, список ошибок в errors."""
    W, H = rec["w"], rec["h"]
    L = [dict(l, cx=(l["x"] + l["w"] / 2) / W, yn=l["y"] / H, t=l["text"].strip()) for l in rec["lines"]]
    out = {"file": os.path.basename(rec["file"]), "errors": []}
    for l in L:
        m = re.match(r"^(\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2})$", l["t"])
        if m and l["yn"] < 0.08 and MON.get(m.group(2).title()):
            out["img_date"] = f"{m.group(3)}-{MON[m.group(2).title()]:02d}-{int(m.group(1)):02d}"
            out["img_time"] = f"{m.group(4)}:{m.group(5)}"
    titles = find_titles(rec["lines"], W, H)
    if len(titles) != 5:
        out["errors"].append(f"заголовков блоков {len(titles)} из 5 — зоны по умолчанию")
    ty = {k: titles.get(k, DEFAULT_TITLES[k]) for k, _ in BLOCKS}
    chart = [l["yn"] for l in L if "динамика" in l["t"].lower()]
    order = sorted(ty.items(), key=lambda kv: kv[1])
    bounds = [(k, y, (order[i + 1][1] if i + 1 < len(order) else (chart[0] if chart else 0.732))) for i, (k, y) in enumerate(order)]
    # шапка: курс доллара $ и фиксинг — в полосе заголовка золота
    band = [l for l in L if abs(l["yn"] - ty["gold"]) < 0.012 and l["cx"] > 0.45]
    for l in band:
        tt = l["t"]
        if "$" in tt and not tt.lower().startswith("курс") and not tt.lower().startswith("фиксинг"):
            out["fixing_usd"] = fixing_value(tt)
        elif to_dec(tt) is not None and l["cx"] < 0.68:
            out["usd_header"] = to_dec(tt)
    for k, y1, y2 in bounds:
        rows = [l for l in L if y1 + 0.004 < l["yn"] < y2 - 0.003]
        dates = [l for l in rows if DATE_RE.match(l["t"])]
        nums = [l for l in rows if to_dec(l["t"]) is not None and not DATE_RE.match(l["t"]) and l["h"] / H >= 0.012]
        cand = {"date_cur": [l for l in dates if l["cx"] < 0.36], "date_1c": [l for l in dates if 0.36 <= l["cx"] < 0.66],
                "cur": [l for l in nums if l["cx"] < 0.33], "c1": [l for l in nums if 0.33 <= l["cx"] < 0.62],
                "dev": [l for l in nums if 0.62 <= l["cx"] < 0.82], "devp": [l for l in nums if l["cx"] >= 0.82]}
        for f in FIELDS:
            lst = cand[f]
            if len(lst) != 1:
                out[f"{k}_{f}"] = None
                if lst:
                    out["errors"].append(f"{k}.{f}: несколько строк {[x['t'] for x in lst]}")
                continue
            out[f"{k}_{f}"] = lst[0]["t"] if f.startswith("date") else to_dec(lst[0]["t"])
    out["titles_y"] = ty
    return out


def check_block(o, k):
    """Ошибки формул блока k: [] — сошлось."""
    cur, c1, dev, devp = (o.get(f"{k}_{f}") for f in ("cur", "c1", "dev", "devp"))
    if None in (cur, c1, dev, devp):
        return [f"{k}: пусто ({', '.join(f for f in FIELDS if o.get(f'{k}_{f}') is None)})"]
    errs = []
    if abs((cur - c1) - dev) > TOL:
        errs.append(f"{k}: отклонение {dev} ≠ {cur} − {c1} = {cur - c1}")
    if c1 != 0:
        exp = (dev / c1 * 100).quantize(D("0.01"))
        if abs(exp - devp) > TOL:
            errs.append(f"{k}: отклонение % {devp} ≠ {exp}")
    return errs


def check(o):
    return [e for k, _ in BLOCKS for e in check_block(o, k)]


def zone(W, H, key, field, title_y):
    x0, x1 = COLS[field]
    y0, y1 = ROWS[field]
    return int(x0 * W), int((title_y + y0) * H), int((x1 - x0) * W), int((y1 - y0) * H)


def crop_ocr(exe, jobs, scale, tmp):
    if not jobs:
        return []
    args = [exe, tmp, str(scale)]
    for p, x, y, w, h in jobs:
        args += [p, str(x), str(y), str(w), str(h)]
    subprocess.run(args, check=True, capture_output=True)
    with open(tmp) as fh:
        return [json.loads(l) for l in fh]


def best_text(rec, field):
    cands = [l["text"].strip() for l in rec["lines"]]
    if field.startswith("date"):
        for t in cands:
            if DATE_RE.match(t):
                return t
        for t in cands:
            m = re.search(r"\d{2}\.\d{2}\.\d{4}", t)
            if m:
                return m.group(0)
        return None
    vals = [(t, to_dec(t)) for t in cands]
    vals = [(t, v) for t, v in vals if v is not None]
    if len(vals) == 1:
        return vals[0][0]
    if len(vals) > 1:
        big = max(rec["lines"], key=lambda l: l["h"])
        return big["text"].strip() if to_dec(big["text"]) is not None else vals[0][0]
    return None


def derive(o):
    """Пустое отклонение или отклонение % восстановить из трёх остальных, если они согласованы; пометить в derived."""
    for k, _ in BLOCKS:
        cur, c1, dev, devp = (o.get(f"{k}_{f}") for f in ("cur", "c1", "dev", "devp"))
        if None not in (cur, c1, dev) and devp is None and c1 != 0 and abs((cur - c1) - dev) <= TOL:
            o[f"{k}_devp"] = (dev / c1 * 100).quantize(D("0.01"))
            o["derived"].append(f"{k}.devp")
        elif None not in (cur, c1, devp) and dev is None and c1 != 0:
            d = cur - c1
            if abs((d / c1 * 100).quantize(D("0.01")) - devp) <= TOL:
                o[f"{k}_dev"] = d
                o["derived"].append(f"{k}.dev")


def parse_with_fallback(rec, crop_exe, tmp):
    o = parse_full(rec)
    o["fallback"], o["derived"] = [], []
    W, H, path = rec["w"], rec["h"], rec["file"]
    for rnd, scale in enumerate((2.5, 4.0)):
        missing = [(k, f) for k, _ in BLOCKS for f in FIELDS if o.get(f"{k}_{f}") is None]
        for e in check(o):
            k = e.split(":")[0]
            if "пусто" not in e:
                missing += [(k, f) for f in ("cur", "c1", "dev", "devp") if (k, f) not in missing]
        if not missing:
            break
        jobs = [(path,) + zone(W, H, k, f, o["titles_y"][k]) for k, f in missing]
        for (k, f), cr in zip(missing, crop_ocr(crop_exe, jobs, scale, tmp)):
            t = best_text(cr, f)
            if t is None:
                continue
            new = t if f.startswith("date") else to_dec(t)
            old = o.get(f"{k}_{f}")
            if old is None or old != new:
                o[f"{k}_{f}"] = new
                o["fallback"].append(f"{k}.{f}: {old} → {new} (×{scale})")
    derive(o)
    o["check"] = check(o)
    return o


# ---------------------------------------------------------------- история курса 1С

def history_1c(rows, key):
    """[(дата установки, курс 1С, первая картинка, где виден)] без повторов подряд; дребезг (возврат к старой паре) остаётся как есть."""
    hist = []
    for x in rows:
        v, d = x.get(f"{key}_c1"), x.get(f"{key}_date_1c")
        if v is None or d is None:
            continue
        if not hist or (hist[-1][0], hist[-1][1]) != (d, v):
            hist.append((d, v, x["img_date"]))
    return hist


def ddmmyyyy_to_iso(s):
    return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"


# ---------------------------------------------------------------- main

def load_export(export_dir):
    with open(os.path.join(export_dir, "result.json")) as fh:
        d = json.load(fh)
    msgs = d["messages"]
    photos = [m for m in msgs if m.get("photo")]
    return d, msgs, photos


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", required=True, help="корень экспорта чата «LL Курсы» (result.json + photos/)")
    ap.add_argument("--sample", type=int, default=0, help="выдать N случайных картинок для сверки глазами (приёмка б)")
    ap.add_argument("--seed", type=int, default=47)
    ap.add_argument("--reocr", action="store_true", help="не брать кэш OCR из data/ll_rates/ocr/, распознать заново")
    args = ap.parse_args(argv)
    export = os.path.expanduser(args.export)
    os.makedirs(OUT_DIR, exist_ok=True)
    ocr_dir = os.path.join(OUT_DIR, "ocr")
    os.makedirs(ocr_dir, exist_ok=True)
    chat, msgs, photo_msgs = load_export(export)
    print(f"чат «{chat.get('name')}» id {chat.get('id')}: сообщений {len(msgs)}, с фото {len(photo_msgs)}, "
          f"{msgs[0]['date'][:10]} … {msgs[-1]['date'][:10]}")
    files = []
    absent = []
    for m in photo_msgs:
        p = os.path.join(export, m["photo"])
        (files if os.path.exists(p) else absent).append(p if os.path.exists(p) else m["photo"])
    if absent:
        print(f"фото из result.json нет на диске: {len(absent)} — {absent[:5]}")
    exes = ensure_binaries(OUT_DIR)
    cache = os.path.join(ocr_dir, "full.jsonl")
    recs = {}
    if os.path.exists(cache) and not args.reocr:
        with open(cache) as fh:
            for line in fh:
                r = json.loads(line)
                recs[r["file"]] = r
    todo = [f for f in files if f not in recs]
    if todo:
        print(f"OCR всей картинки: {len(todo)} файлов (Vision, параллельно в 4 процесса) …", flush=True)
        n = 4
        k = (len(todo) + n - 1) // n
        procs = []
        for i in range(n):
            part = todo[i * k:(i + 1) * k]
            if not part:
                continue
            outp = os.path.join(ocr_dir, f"part_{i}.jsonl")
            procs.append((outp, subprocess.Popen([exes["vision_ocr"], outp] + part, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)))
        for outp, p in procs:
            _, err = p.communicate()
            if err:
                print(err.decode()[:500])
            with open(outp) as fh:
                for line in fh:
                    r = json.loads(line)
                    recs[r["file"]] = r
        with open(cache, "w") as fh:
            for f in files:
                if f in recs:
                    fh.write(json.dumps(recs[f], ensure_ascii=False) + "\n")
    tmp = os.path.join(ocr_dir, "crop_tmp.jsonl")
    results, skipped = [], []
    for f in files:
        r = recs.get(f)
        if r is None:
            skipped.append((os.path.basename(f), "OCR не вернул запись"))
            continue
        if r["w"] < MIN_BOT_WIDTH:
            skipped.append((os.path.basename(f), f"{r['w']}×{r['h']} — не картинка бота"))
            continue
        results.append(parse_with_fallback(r, exes["vision_crop"], tmp))

    # дата картинки: из шапки, иначе из имени файла photo_N@DD-MM-YYYY_hh-mm-ss
    for o in results:
        m = re.search(r"@(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})", o["file"])
        o["msg_date"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
        if not o.get("img_date"):
            o["img_date"] = o["msg_date"]
            o["derived"].append("img_date←имя файла")
    results.sort(key=lambda o: (o["img_date"] or "", o["file"]))

    # ---- csv
    cols = ["img_date", "img_time", "msg_date", "file", "usd_header", "fixing_usd"]
    for k, _ in BLOCKS:
        cols += [f"{k}_{f}" for f in FIELDS]
    cols += ["fallback", "derived", "check"]
    csv_path = os.path.join(OUT_DIR, "ll_rates.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for o in results:
            w.writerow([("; ".join(o[c]) if isinstance(o.get(c), list) else ("" if o.get(c) is None else str(o[c]))) for c in cols])

    # ---- приёмка (а): формулы
    bad = [o for o in results if o["check"]]
    print(f"\nразобрано картинок бота: {len(results)}; не бота (пропущены): {len(skipped)}")
    for s in skipped:
        print(f"  пропуск: {s[0]} — {s[1]}")
    print(f"второй проход кропом понадобился: {sum(1 for o in results if o['fallback'])} картинок; восстановлено из формулы: "
          f"{sum(1 for o in results if any(d.endswith('.devp') or d.endswith('.dev') for d in o['derived']))} "
          f"({Counter(d for o in results for d in o['derived'])})")
    print(f"приёмка (а) формулы: промахов {len(bad)} из {len(results)}")
    for o in bad:
        print(f"  {o['file']} {o['img_date']}: {o['check']}")
    sizes = Counter()
    for f in files:
        r = recs.get(f)
        if r:
            sizes[(r["w"], r["h"])] += 1
    print("размеры картинок:", dict(sizes.most_common()))

    # ---- приёмка (в): полнота по месяцам
    by_month_json = Counter(m["date"][:7] for m in photo_msgs)
    by_month_ok = Counter(o["img_date"][:7] for o in results if not o["check"])
    by_month_bot = Counter(o["img_date"][:7] for o in results)
    print("\nприёмка (в) полнота: месяц | фото в result.json | картинок бота | разобрано без промахов | рабочих дней без картинки")
    import calendar
    from datetime import date
    days_with = {o["img_date"] for o in results}
    gaps_all = []
    for ym in sorted(by_month_json):
        y, mo = int(ym[:4]), int(ym[5:7])
        first_img = min(o["img_date"] for o in results)
        last_img = max(o["img_date"] for o in results)
        wd = [date(y, mo, d) for d in range(1, calendar.monthrange(y, mo)[1] + 1) if date(y, mo, d).weekday() < 5]
        wd = [d for d in wd if first_img <= d.isoformat() <= last_img]
        gaps = [d.isoformat() for d in wd if d.isoformat() not in days_with]
        gaps_all += gaps
        print(f"  {ym} | {by_month_json[ym]:3} | {by_month_bot.get(ym, 0):3} | {by_month_ok.get(ym, 0):3} | {len(gaps):2}"
              + (f"  ({', '.join(g[5:] for g in gaps)})" if 0 < len(gaps) <= 8 else (f"  ({len(gaps)} дат)" if gaps else "")))
    print(f"итого рабочих дней без картинки: {len(gaps_all)} (праздники РФ сюда тоже входят)")

    # ---- история курса 1С
    rows = [o for o in results if o.get("img_date")]
    hist_path = os.path.join(OUT_DIR, "ll_rates_1c_history.csv")
    with open(hist_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rate", "set_date", "value_1c", "first_seen_img"])
        for key in ("gold", "silver", "usd", "kzt", "scrap"):
            for d, v, seen in history_1c(rows, key):
                w.writerow([key, ddmmyyyy_to_iso(d), str(v), seen])
    for key in ("gold", "silver", "usd"):
        h = history_1c(rows, key)
        print(f"\nистория курса 1С «{key}»: установок {len(h)}")
        iso = [ddmmyyyy_to_iso(d) for d, _, _ in h]
        from datetime import date as _d
        gaps = [(_d.fromisoformat(b) - _d.fromisoformat(a)).days for a, b in zip(iso, iso[1:])]
        gaps_pos = sorted(g for g in gaps if g > 0)
        if gaps_pos:
            med = gaps_pos[len(gaps_pos) // 2]
            print(f"  интервал между установками, дней: медиана {med}, мин {gaps_pos[0]}, макс {gaps_pos[-1]}, "
                  f"квартили {gaps_pos[len(gaps_pos) // 4]} / {gaps_pos[3 * len(gaps_pos) // 4]}; шагов назад по дате (дребезг) {sum(1 for g in gaps if g < 0)}")
        print("  первые 10:", [(d, str(v)) for d, v, _ in h[:10]])
        print("  последние 10:", [(d, str(v)) for d, v, _ in h[-10:]])
    print(f"\nзаписано: {csv_path} ({len(results)} строк), {hist_path}; сырьё OCR — {ocr_dir}/ ; db_writes = 0, обращений к API 0")

    if args.sample:
        rnd = random.Random(args.seed)
        pick = rnd.sample([o for o in results], args.sample)
        print(f"\nприёмка (б): {args.sample} случайных картинок (seed {args.seed}) — открыть и сверить с csv:")
        for o in sorted(pick, key=lambda o: o["img_date"]):
            print(f"  {o['file']} | {o['img_date']} | золото {o['gold_cur']} / 1С {o['gold_c1']} от {o['gold_date_1c']} / {o['gold_dev']} / {o['gold_devp']} | "
                  f"серебро {o['silver_cur']} / {o['silver_c1']} от {o['silver_date_1c']} | доллар {o['usd_cur']} / {o['usd_c1']} от {o['usd_date_1c']} | "
                  f"тенге {o['kzt_cur']} / {o['kzt_c1']} | лом {o['scrap_cur']} / {o['scrap_c1']} | $ {o.get('usd_header')} фиксинг {o.get('fixing_usd')}")


if __name__ == "__main__":
    main()
