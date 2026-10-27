"""Прайс себестоимости РБХ (вторая задача RBH, дополнение 23.10) → строки article_unit_costs.

Файл владельца `data/rbh_costs/rbh_cost_20261015.xlsx`, лист «Лист1», колонки: КодID, Артикул, Бренд, «С-ть до МП, руб/шт», Sku OZ.
Правила (решение советника 23.10, владелец может поправить):
  • ключ — «Артикул» поставщика: у Ozon это offer_id карточки, у WB — vendorCode; 116 артикулов записаны строкой, остальные числом —
    читаем как текст (dtype=str), хвост «.0» у чисел снимаем, ведущие нули не теряем, lower() — ключ, как у 1С;
  • «С-ть до МП» — С НДС (слово владельца 22.10): в unit_cost пишем БЕЗ НДС = цена ÷ vat_rate (1,22), округление до копейки;
    исходная цена с НДС в таблицу НЕ пишется (unit_cost_uniform = null): у WB-читателей «единая» по базе артикула стоит в лестнице
    ключей (exact → size → … → uniform_base) и при артикуле с суффиксом («51050-W») молча взяла бы цену с НДС; цена с НДС
    восстановима как unit_cost × 1,22 (до копейки) и остаётся в файле прайса (source_file / source_row);
  • «Бренд» → analytics_group (42 бренда; у карточек Ozon бренд везде Beautyhome.me, источник бренда для книг — прайс);
  • variant_type = main (check-constraint таблицы), article_1c = артикул, Sku OZ не хранится — только сверяется с каталогом в плане;
  • один артикул с РАЗНОЙ ценой — конфликт, не грузится и называется (в файле 15.10 артикулы уникальны — 0 конфликтов);
  • пустой артикул / пустая или нулевая цена — пропуск с подсчётом;
  • артикул «0» (прайс «до 01.09»: 159 строк, у 57 есть Sku OZ) — не артикул: пропуск с подсчётом (zero_article, zero_article_sku_oz),
    сопоставление по Sku OZ — отдельный план, не по умолчанию;
  • боковой блок «Артикул поставщика | Баркод | Себестоимость по умолчанию» (прайс «до 01.09», колонки I–K) — read_side_block: только
    артикулы, которых нет в основном блоке, и только с одной ценой (слово владельца 27.10); цена в нём — та же «с НДС», округлённая до рубля.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os

import pandas as pd

Q = Decimal("0.01")
COLUMNS = {"code": ("КодID",), "article": ("Артикул",), "brand": ("Бренд",),
           "cost": ("С-ть до МП, руб/шт", "С-ть до МП руб/шт", "Себестоимость"), "sku_oz": ("Sku OZ", "SKU OZ", "Sku Ozon")}
REQUIRED = ("article", "cost")


def _norm_header(name):
    return " ".join(str(name).split()).casefold()


def resolve_columns(columns):
    by_norm = {_norm_header(c): c for c in columns}
    out = {role: next((by_norm[_norm_header(a)] for a in aliases if _norm_header(a) in by_norm), None) for role, aliases in COLUMNS.items()}
    missing = [r for r in REQUIRED if out[r] is None]
    if missing:
        raise RuntimeError(f"в прайсе нет обязательных колонок {missing}; шапка: {list(columns)}")
    return out


def clean_text(value):
    """Текст ячейки: None/NaN → None; «930281.0» → «930281»; пробелы по краям сняты; ведущие нули не трогаем."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def money(value):
    s = clean_text(value)
    if s is None:
        return None
    try:
        d = Decimal(s.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    return d if d > 0 else None


def read_price(path, snapshot_date, marketplace_code="ozon", vat_rate=Decimal("1.22"), sheet_name=0):
    """(rows, conflicts, stats): rows — словари под upsert article_unit_costs; conflicts — [(артикул, [цены], [строки файла])]."""
    vat_rate = Decimal(str(vat_rate)) if vat_rate else None
    df = pd.read_excel(path, sheet_name=sheet_name, dtype=str)
    cols = resolve_columns(df.columns)
    source_file = os.path.basename(path)
    stats = {"rows_read": len(df), "empty_article": 0, "empty_cost": 0, "conflicts": 0, "duplicates_same_cost": 0, "zero_article": 0, "zero_article_sku_oz": 0,
             "sku_oz_filled": 0, "sku_oz_duplicates": [], "brands": defaultdict(lambda: [0, Decimal(0), Decimal(0)]),
             "sum_with_vat": Decimal(0), "sum_without_vat": Decimal(0), "vat_rate": vat_rate, "columns": cols}
    by_norm = defaultdict(list)
    sku_seen = defaultdict(list)
    for idx, row in df.iterrows():
        article = clean_text(row.get(cols["article"]))
        if article is None:
            stats["empty_article"] += 1
            continue
        if article == "0":                              # не артикул (прайс «до 01.09»): не грузится, называется числом
            stats["zero_article"] += 1
            stats["zero_article_sku_oz"] += 1 if (cols["sku_oz"] and clean_text(row.get(cols["sku_oz"]))) else 0
            continue
        price = money(row.get(cols["cost"]))
        if price is None:
            stats["empty_cost"] += 1
            continue
        price = price.quantize(Q, rounding=ROUND_HALF_UP)
        without = (price / vat_rate).quantize(Q, rounding=ROUND_HALF_UP) if vat_rate else price
        brand = clean_text(row.get(cols["brand"])) if cols["brand"] else None
        sku_oz = clean_text(row.get(cols["sku_oz"])) if cols["sku_oz"] else None
        if sku_oz:
            stats["sku_oz_filled"] += 1
            sku_seen[sku_oz].append(article)
        by_norm[article.lower()].append({
            "marketplace_code": marketplace_code, "offer_id": article, "offer_id_norm": article.lower(),
            "snapshot_date": snapshot_date, "unit_cost": str(without), "unit_cost_uniform": None, "currency": "RUB",
            "variant_type": "main", "article_1c": article, "size": None, "insert_category": None, "product_kind": None,
            "metal_fineness": None, "weight_g": None, "discontinued": False, "source_file": source_file, "source_row": int(idx) + 2,
            "analytics_group": brand, "_sku_oz": sku_oz, "_brand": brand, "_price_with_vat": str(price),
        })
    stats["sku_oz_duplicates"] = sorted((s, sorted(set(arts))) for s, arts in sku_seen.items() if len(set(arts)) > 1)   # один Sku OZ у РАЗНЫХ артикулов
    rows, conflicts = [], []
    for norm, cands in by_norm.items():
        prices = sorted({c["_price_with_vat"] for c in cands}, key=Decimal)
        if len(prices) > 1:
            stats["conflicts"] += 1
            conflicts.append((cands[0]["offer_id"], prices, sorted(c["source_row"] for c in cands)))
            continue
        if len(cands) > 1:
            stats["duplicates_same_cost"] += len(cands) - 1
        r = cands[0]
        rows.append(r)
        b = stats["brands"][r["_brand"] or "(без бренда)"]
        b[0] += 1; b[1] += Decimal(r["unit_cost"]); b[2] += Decimal(r["_price_with_vat"])
        stats["sum_without_vat"] += Decimal(r["unit_cost"]); stats["sum_with_vat"] += Decimal(r["_price_with_vat"])
    stats["brands"] = dict(stats["brands"])
    return rows, conflicts, stats


SIDE_ARTICLE, SIDE_COST = "артикул поставщика", "себестоимость по умолчанию"


def read_side_block(path, main_norms, snapshot_date, marketplace_code="ozon", vat_rate=Decimal("1.22"), sheet_name=0, scan_rows=10):
    """Боковой блок прайса (карта артикулов WB с суффиксами): (rows, stats). Шапка ищется в первых scan_rows строках листа по именам
    «Артикул поставщика» и «Себестоимость по умолчанию». В строки идут только артикулы, которых нет в main_norms (основной блок), с одной
    ценой; двойные цены и совпадающие с основным — считаются и называются. source_file — «<файл>#боковой блок»."""
    vat_rate = Decimal(str(vat_rate)) if vat_rate else None
    df = pd.read_excel(path, sheet_name=sheet_name, dtype=str, header=None)
    head = None
    for i in range(min(scan_rows, len(df))):
        cells = {_norm_header(v): j for j, v in enumerate(df.iloc[i]) if isinstance(v, str)}
        if SIDE_ARTICLE in cells and SIDE_COST in cells:
            head = (i, cells[SIDE_ARTICLE], cells[SIDE_COST])
            break
    stats = {"side_rows": 0, "side_keys": 0, "in_main": 0, "double_price": [], "added": 0, "header": head}
    if head is None:
        return [], stats
    i0, ja, jc = head
    prices, rows_of = defaultdict(set), defaultdict(list)
    for idx in range(i0 + 1, len(df)):
        art, price = clean_text(df.iat[idx, ja]), money(df.iat[idx, jc])
        if art is None or price is None:
            continue
        stats["side_rows"] += 1
        prices[art.lower()].add(price.quantize(Q, rounding=ROUND_HALF_UP)); rows_of[art.lower()].append((art, idx + 1))
    stats["side_keys"] = len(prices)
    out, source_file = [], os.path.basename(path) + "#боковой блок"
    for norm, ps in sorted(prices.items()):
        if norm in main_norms:
            stats["in_main"] += 1
            continue
        if len(ps) > 1:
            stats["double_price"].append((norm, sorted(str(p) for p in ps)))
            continue
        price = next(iter(ps))
        without = (price / vat_rate).quantize(Q, rounding=ROUND_HALF_UP) if vat_rate else price
        art, src = rows_of[norm][0]
        out.append({"marketplace_code": marketplace_code, "offer_id": art, "offer_id_norm": norm, "snapshot_date": snapshot_date,
                    "unit_cost": str(without), "unit_cost_uniform": None, "currency": "RUB", "variant_type": "main", "article_1c": art,
                    "size": None, "insert_category": None, "product_kind": None, "metal_fineness": None, "weight_g": None, "discontinued": False,
                    "source_file": source_file, "source_row": src, "analytics_group": None, "_sku_oz": None, "_brand": None, "_price_with_vat": str(price)})
    stats["added"] = len(out)
    return out, stats


def db_rows(rows):
    """Строки под upsert: служебные поля (_sku_oz, _brand) сняты."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def sku_oz_map(rows):
    """{Sku OZ: артикул} по строкам прайса (для сверки с каталогом кабинета)."""
    return {r["_sku_oz"]: r["offer_id"] for r in rows if r.get("_sku_oz")}
