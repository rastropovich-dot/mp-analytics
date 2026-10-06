#!/usr/bin/env python3
"""Сводные владельца с фильтрами — на наших листах «Данные» (тридцать восьмая §4): пост-обработка книги «Фин рез» на уровне zip / XML.

    venv/bin/python3 scripts/finrez_pivots.py data/reports/finrez_2026-04_2026-09.xlsx [--out …] [--check]
    venv/bin/python3 scripts/finrez_pivots.py --check-only data/reports/finrez_2026-04_2026-09.xlsx

Из оригиналов владельца (data/owner_finrez_*_pivot.xlsx: лист со сводной, pivotTable, pivotCacheDefinition, для заказов — срезы) в нашу книгу
добавляются три листа — «Сводная Ozon выкупы», «Сводная WB выкупы», «Сводная заказы». Источник кэша меняется с внешнего (Power Query) на
worksheetSource: лист «Данные …», ref = ровно колонки ИСТОЧНИКА владельца (13 / 14 / 29 — поля кэша без fieldGroup; «Дни …» / «Месяцы» — группировка
сводной по дате, Excel строит их сам; наши колонки справа в диапазон не входят — cacheFields не меняются; rangePr группировок — startDate / endDate = окно
книги (date_range): без дат, с autoStart / autoEnd, Excel кэш НЕ открывает — «Ошибка в части содержимого», все сводные, кэши и срезы выбрасываются (сорок первая §2,
доказано на маленьких книгах; групповые элементы Excel перестраивает при refreshOnLoad сам); refreshOnLoad="1", recordCount="0", pivotCacheRecords пустой: Excel пересчитывает сводную при
открытии по нашим данным и сохраняет все фильтры страниц и срезы. connections.xml / queryTables / customXml запроса не переносятся.

Что правится в копируемых частях: ячейки листа сводной — стили сняты (индексы его styles.xml), строки из sharedStrings развёрнуты в inlineStr,
примечания (legacyDrawing) и настройки принтера сняты; форматы сводной (dxfId) и её numFmtId ≥ 164 переномерованы и добавлены в наш styles.xml;
у срезов tabId = sheetId нового листа. openpyxl не используется (он теряет срезы и может испортить сводные). Пересчёт делает только Excel.

--check: zip целый (testzip), все новые части — корректный XML, r:id / cacheId / имена листов сходятся, шапка листа-источника = cacheFields,
worksheetSource ref = число строк листа, tabId срезов = sheetId, openpyxl открывает (read_only). Код 1 — если что-то не сошлось.
"""
import argparse
import os
import re
import shutil
import sys
import warnings
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cabinet  # noqa: E402  — только профиль: где лежат образцы сводных владельца (OWNER_PIVOT_FILES в каталоге данных кабинета)
_PROFILE = cabinet.profile()
_OWNER_FILES = {k: cabinet.data_path(v, prof=_PROFILE) for k, v in _PROFILE.OWNER_PIVOT_FILES.items()}
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
REL_MS = "http://schemas.microsoft.com/office/2007/relationships/"
CT = {
    "worksheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "pivotTable": "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotTable+xml",
    "pivotCacheDefinition": "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheDefinition+xml",
    "pivotCacheRecords": "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheRecords+xml",
    "slicer": "application/vnd.ms-excel.slicer+xml",
    "slicerCache": "application/vnd.ms-excel.slicerCache+xml",
    "drawing": "application/vnd.openxmlformats-officedocument.drawing+xml",
}
SPECS = [
    # ncols — колонки ИСТОЧНИКА владельца: поля кэша без fieldGroup («Дни …» / «Месяцы» — группировка сводной по дате, Excel строит их сам)
    {"owner": _OWNER_FILES["ozon_buyouts"], "owner_sheet": "Вывод данных", "new_sheet": "Сводная Ozon выкупы", "source_sheet": "Данные Ozon выкупы", "ncols": 13, "data_fields": 2},
    # WB: 14 колонок владельца + наша «Эквайринг, ₽» пятнадцатой (O) — восьмое поле значений сводной, колонка V читает его (сорок четвёртая §2);
    # «Месяцы» (группировка) в источник не попадают, наши остальные колонки — правее O
    {"owner": _OWNER_FILES["wb_buyouts"], "owner_sheet": "Свод", "new_sheet": "Сводная WB выкупы", "source_sheet": "Данные WB выкупы", "ncols": 15,
     "extra_field": {"name": "Эквайринг, ₽", "index": 14}, "data_fields": 8},
    # заказы: 21 колонка источника (Дата … День); «Месяцы» — группировка, «Маржа …» … «Фин.рез %» (8) — ВЫЧИСЛЯЕМЫЕ поля сводной (formula в кэше),
    # Excel считает их сам из полей источника по именам; в нашем листе одноимённые колонки остаются справа от ref справочно
    {"owner": _OWNER_FILES["orders"], "owner_sheet": "Свод", "new_sheet": "Сводная заказы", "source_sheet": "Данные заказы", "ncols": 21, "data_fields": 13},
]


def source_fields(cache_xml):
    """Имена полей кэша из источника и имена полей НЕ из источника (databaseField="0"): групповые («Месяцы» / «Дни …» — Excel строит из даты)
    и вычисляемые (formula= в кэше, у заказов их 8: «Маржа …» … «Фин.рез %»). Поле «Дата» источника тоже несёт fieldGroup (сгруппировано по
    дням, par — «Месяцы»), но в источнике есть — признак только databaseField."""
    src, grp = [], []
    for m in re.finditer(r'<cacheField ([^>]*)>', cache_xml):
        attrs = m.group(1)
        name = re.search(r'name="([^"]+)"', attrs).group(1).replace("&amp;", "&")
        (grp if 'databaseField="0"' in attrs else src).append(name)
    return src, grp
EMPTY_RECORDS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<pivotCacheRecords xmlns="%s" xmlns:r="%s" count="0"/>' % (NS_MAIN, NS_R)).encode()


def col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def rels_of(z, part):
    """{rId: (type, target абсолютный путь в zip)} для части part (например xl/worksheets/sheet3.xml)."""
    d, f = os.path.split(part)
    rp = f"{d}/_rels/{f}.rels"
    if rp not in z.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(z.read(rp)).findall(f"{{{NS_PKG}}}Relationship"):
        t = rel.get("Target")
        if t.startswith("/"):
            path = t[1:]
        else:
            path = os.path.normpath(os.path.join(d, t)).replace("\\", "/")
        out[rel.get("Id")] = (rel.get("Type"), path, rel.get("TargetMode"))
    return out


def workbook_sheets(z):
    """[(name, sheetId, rId)] и rels книги."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = [(s.get("name"), int(s.get("sheetId")), s.get(f"{{{NS_R}}}id")) for s in wb.find(f"{{{NS_MAIN}}}sheets")]
    return sheets, rels_of(z, "xl/workbook.xml")


def shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{{{NS_MAIN}}}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{{{NS_MAIN}}}t")))
    return out


def owner_parts(z, sheet_name):
    """Части сводной владельца: sheet, pivotTable, cacheDef, records, drawing, slicers, slicerCaches, definedNames срезов, ext x14 книги."""
    sheets, wrels = workbook_sheets(z)
    match = [s for s in sheets if s[0] == sheet_name]
    if not match:
        raise SystemExit(f"в {z.filename} нет листа «{sheet_name}»; есть {[s[0] for s in sheets]}")
    _name, sheet_id, rid = match[0]
    sheet_part = wrels[rid][1]
    srels = rels_of(z, sheet_part)
    pt = [p for t, p, _m in srels.values() if t == REL + "pivotTable"]
    if len(pt) != 1:
        raise SystemExit(f"{z.filename}: на листе «{sheet_name}» сводных {len(pt)}, ожидалась одна")
    prels = rels_of(z, pt[0])
    cache = [p for t, p, _m in prels.values() if t == REL + "pivotCacheDefinition"][0]
    drawing = [p for t, p, _m in srels.values() if t == REL + "drawing"]
    slicers = [p for t, p, _m in srels.values() if t == REL_MS + "slicer"]
    slicer_caches = [p for t, p, _m in wrels.values() if t == REL_MS + "slicerCache"]
    wb_xml = z.read("xl/workbook.xml").decode("utf-8")
    dnames = re.findall(r'<definedName name="Срез_[^"]*"[^>]*>[^<]*</definedName>', wb_xml)
    return {"sheet_id": sheet_id, "sheet": sheet_part, "pivot": pt[0], "cache": cache, "drawing": drawing[0] if drawing else None,
            "slicers": slicers, "slicer_caches": slicer_caches, "defined_names": dnames, "strings": shared_strings(z)}


def inline_strings(xml, strings):
    """t="s" → inlineStr по таблице строк владельца; индексы стилей (s= у ячеек, style= у колонок и строк) остаются — их перенумеровывает restyle_sheet
    после merge_styles (сорок вторая §3); примечания (legacyDrawing), настройки принтера и tabSelected сняты."""
    def repl(m):
        attrs, idx = m.group(1), int(m.group(2))
        attrs = re.sub(r'\s+t="s"', "", attrs)
        text = strings[idx] if idx < len(strings) else ""
        return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    xml = re.sub(r'<c((?:\s+[a-zA-Z:]+="[^"]*")*?\s+t="s"(?:\s+[a-zA-Z:]+="[^"]*")*)><v>(\d+)</v></c>', repl, xml)
    xml = re.sub(r'<legacyDrawing [^>]*/>', "", xml)
    xml = re.sub(r'<pageSetup [^>]*/>', "", xml)
    xml = re.sub(r'\s+tabSelected="1"', "", xml)
    return xml


def remap_formats(xml, dxf_offset, numfmt_map):
    xml = re.sub(r'dxfId="(\d+)"', lambda m: f'dxfId="{int(m.group(1)) + dxf_offset}"', xml)
    xml = re.sub(r'numFmtId="(\d+)"', lambda m: f'numFmtId="{numfmt_map.get(int(m.group(1)), int(m.group(1)))}"', xml)
    return xml


def _append_children(styles, tag, children):
    """Дописывает дочерние элементы в <tag count="N">…</tag> нашего styles.xml, поднимая count; пустой <tag count="0"/> раскрывается."""
    if not children:
        return styles
    m = re.search(rf'<{tag}\b([^>]*?)\s*/>', styles) or re.search(rf'<{tag}\b([^>]*)>(.*?)</{tag}>', styles, re.S)
    if not m:
        raise SystemExit(f"в нашем styles.xml нет <{tag}>")
    attrs = m.group(1)
    body = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
    have = int(re.search(r'count="(\d+)"', attrs).group(1)) if re.search(r'count="(\d+)"', attrs) else len(re.findall(r"<(?:font|fill|border|xf|cellStyle|dxf)\b", body))
    attrs = re.sub(r'count="\d+"', f'count="{have + len(children)}"', attrs) if 'count="' in attrs else attrs + f' count="{have + len(children)}"'
    return styles.replace(m.group(0), f"<{tag}{attrs}>{body}{''.join(children)}</{tag}>", 1)


def _children(styles, tag, child):
    m = re.search(rf'<{tag}\b[^>]*>(.*?)</{tag}>', styles, re.S)
    return re.findall(rf'<{child}\b[^>]*/>|<{child}\b[^>]*>.*?</{child}>', m.group(1), re.S) if m else []


def merge_styles(our_styles, owner_styles):
    """Сливает styles.xml владельца в наш с перенумерацией (сорок вторая §3): numFmt ≥ 164, fonts, fills, borders, cellStyleXfs, cellXfs, cellStyles
    (кроме builtinId 0 и уже имеющихся имён), dxfs. Возвращает (styles, dxf_offset, numfmt_map, xf_map) — xf_map: индекс cellXfs владельца →
    наш (0 → 0: «Обычный» есть у всех), им перенумеровываются s= ячеек и style= колонок листа (restyle_sheet) и dxfId / numFmtId сводной."""
    our_styles, dxf_offset, numfmt_map = _merge_numfmts_dxfs(our_styles, owner_styles)
    font_off, fill_off, border_off = (len(_children(our_styles, t, c)) for t, c in (("fonts", "font"), ("fills", "fill"), ("borders", "border")))
    our_styles = _append_children(our_styles, "fonts", _children(owner_styles, "fonts", "font"))
    our_styles = _append_children(our_styles, "fills", _children(owner_styles, "fills", "fill"))
    our_styles = _append_children(our_styles, "borders", _children(owner_styles, "borders", "border"))

    def remap_xf(xf, with_xfid):
        xf = re.sub(r'numFmtId="(\d+)"', lambda m: f'numFmtId="{numfmt_map.get(int(m.group(1)), int(m.group(1)))}"', xf)
        xf = re.sub(r'fontId="(\d+)"', lambda m: f'fontId="{int(m.group(1)) + font_off}"', xf)
        xf = re.sub(r'fillId="(\d+)"', lambda m: f'fillId="{int(m.group(1)) + fill_off}"', xf)
        xf = re.sub(r'borderId="(\d+)"', lambda m: f'borderId="{int(m.group(1)) + border_off}"', xf)
        if with_xfid:
            xf = re.sub(r'xfId="(\d+)"', lambda m: f'xfId="{csx_map.get(int(m.group(1)), 0)}"', xf)
        return xf
    # именованные стили: индекс 0 («Обычный») — наш 0; остальные дописываются
    csx_owner = _children(owner_styles, "cellStyleXfs", "xf")
    csx_have = len(_children(our_styles, "cellStyleXfs", "xf"))
    csx_map = {0: 0}
    new_csx = []
    for i, xf in enumerate(csx_owner):
        if i == 0:
            continue
        csx_map[i] = csx_have + len(new_csx)
        new_csx.append(remap_xf(xf, False))
    our_styles = _append_children(our_styles, "cellStyleXfs", new_csx)
    our_names = set(re.findall(r'<cellStyle name="([^"]+)"', our_styles))
    new_cs = []
    for cs in _children(owner_styles, "cellStyles", "cellStyle"):
        name = re.search(r'name="([^"]+)"', cs).group(1)
        xfid = int(re.search(r'xfId="(\d+)"', cs).group(1))
        if 'builtinId="0"' in cs or name in our_names or xfid not in csx_map or xfid == 0:
            continue
        new_cs.append(re.sub(r'xfId="\d+"', f'xfId="{csx_map[xfid]}"', cs))
        our_names.add(name)
    our_styles = _append_children(our_styles, "cellStyles", new_cs)
    xfs_owner = _children(owner_styles, "cellXfs", "xf")
    xf_have = len(_children(our_styles, "cellXfs", "xf"))
    xf_map = {0: 0}
    new_xfs = []
    for i, xf in enumerate(xfs_owner):
        if i == 0:
            continue
        xf_map[i] = xf_have + len(new_xfs)
        new_xfs.append(remap_xf(xf, True))
    our_styles = _append_children(our_styles, "cellXfs", new_xfs)
    return our_styles, dxf_offset, numfmt_map, xf_map


def restyle_sheet(xml, xf_map):
    """s= у ячеек и style= у колонок / строк листа владельца → наши индексы cellXfs (merge_styles)."""
    xml = re.sub(r'\ss="(\d+)"', lambda m: f' s="{xf_map.get(int(m.group(1)), 0)}"', xml)
    xml = re.sub(r'\sstyle="(\d+)"', lambda m: f' style="{xf_map.get(int(m.group(1)), 0)}"', xml)
    return xml


def _merge_numfmts_dxfs(our_styles, owner_styles):
    """Добавляет в наш styles.xml numFmt ≥ 164 и dxfs владельца; возвращает (styles, dxf_offset, numfmt_map)."""
    own_ids = [int(x) for x in re.findall(r'<numFmt numFmtId="(\d+)"', our_styles)]
    next_id = max(own_ids + [163]) + 1
    numfmt_map, new_fmts = {}, []
    for m in re.finditer(r'<numFmt numFmtId="(\d+)" formatCode="([^"]*)"/>', owner_styles):
        fid, code = int(m.group(1)), m.group(2)
        if fid < 164 or fid in numfmt_map:
            continue
        numfmt_map[fid] = next_id
        new_fmts.append(f'<numFmt numFmtId="{next_id}" formatCode="{code}"/>')
        next_id += 1
    if new_fmts:
        m = re.search(r'<numFmts count="(\d+)">', our_styles)
        if m:
            n = int(m.group(1)) + len(new_fmts)
            our_styles = our_styles.replace(m.group(0), f'<numFmts count="{n}">', 1)
            our_styles = our_styles.replace("</numFmts>", "".join(new_fmts) + "</numFmts>", 1)
        else:
            our_styles = our_styles.replace("<fonts", f'<numFmts count="{len(new_fmts)}">' + "".join(new_fmts) + "</numFmts><fonts", 1)
    dxfs = re.search(r'<dxfs count="(\d+)">(.*?)</dxfs>', owner_styles, re.S)
    owner_dxf = re.findall(r'<dxf>.*?</dxf>', dxfs.group(2), re.S) if dxfs else []
    owner_dxf = [re.sub(r'numFmtId="(\d+)"', lambda m: f'numFmtId="{numfmt_map.get(int(m.group(1)), int(m.group(1)))}"', d) for d in owner_dxf]
    m = re.search(r'<dxfs count="(\d+)"\s*/>|<dxfs count="(\d+)">(.*?)</dxfs>', our_styles, re.S)
    if m:
        have = int(m.group(1) or m.group(2))
        body = (m.group(3) or "") + "".join(owner_dxf)
        our_styles = our_styles.replace(m.group(0), f'<dxfs count="{have + len(owner_dxf)}">{body}</dxfs>', 1)
        offset = have
    else:
        offset = 0
        block = f'<dxfs count="{len(owner_dxf)}">' + "".join(owner_dxf) + "</dxfs>"
        if "<tableStyles" in our_styles:
            our_styles = our_styles.replace("<tableStyles", block + "<tableStyles", 1)
        elif "</cellStyles>" in our_styles:
            our_styles = our_styles.replace("</cellStyles>", "</cellStyles>" + block, 1)
        else:
            our_styles = our_styles.replace("</cellXfs>", "</cellXfs>" + block, 1)
    return our_styles, offset, numfmt_map


def add_data_field(pt_xml, cd_xml, name, index):
    """Восьмое (и т. д.) поле значений сводной из колонки источника, которой у владельца не было (сорок четвёртая §2: «Эквайринг, ₽» у WB).
    Кэш: cacheField name числовой (sharedItems с атрибутами типа, как у соседей) на позиции index среди полей — до полей группировки
    (databaseField="0"), поэтому их номера и ссылки на них (fieldGroup par / base) сдвигаются на 1. Сводная: pivotField dataField на той же
    позиции (номера полей в rowFields / colFields / pageFields / dataFields ≥ index сдвигаются; номера ЭЛЕМЕНТОВ в items / rowItems / colItems
    не трогаются), dataField sum последним, colItems — элементом с номером нового поля, location — на колонку шире."""
    fields = re.findall(r'<cacheField\b[^>]*?(?:/>|>.*?</cacheField>)', cd_xml, re.S)
    if index > len(fields) or any(re.search(r'name="%s"' % re.escape(name), f) for f in fields):
        raise SystemExit(f"add_data_field: поле «{name}» уже есть или index {index} вне полей ({len(fields)})")
    def shift_cd(m):
        n = int(m.group(2)); return f'{m.group(1)}="{n + 1 if n >= index else n}"'
    fields = [re.sub(r'\b(par|base)="(\d+)"', shift_cd, f) for f in fields]
    fields.insert(index, f'<cacheField name="{escape(name)}" numFmtId="0"><sharedItems containsString="0" containsBlank="1" containsNumber="1" minValue="0" maxValue="0"/></cacheField>')
    cd_xml = re.sub(r'<cacheFields count="\d+">.*?</cacheFields>', lambda m: f'<cacheFields count="{len(fields)}">' + "".join(fields) + "</cacheFields>", cd_xml, count=1, flags=re.S)
    pfs = re.findall(r'<pivotField\b[^>]*?(?:/>|>.*?</pivotField>)', pt_xml, re.S)
    pfs.insert(index, '<pivotField dataField="1" showAll="0"/>')
    pt_xml = re.sub(r'<pivotFields count="\d+">.*?</pivotFields>', lambda m: f'<pivotFields count="{len(pfs)}">' + "".join(pfs) + "</pivotFields>", pt_xml, count=1, flags=re.S)
    def shift_field(m):
        n = int(m.group(2)); return f'{m.group(1)}="{n + 1 if n >= index else n}"'
    for tag in ("rowFields", "colFields"):
        pt_xml = re.sub(rf'<{tag}\b.*?</{tag}>', lambda m: re.sub(r'\b(x)="(-?\d+)"', lambda mm: shift_field(mm) if int(mm.group(2)) >= 0 else mm.group(0), m.group(0)), pt_xml, count=1, flags=re.S)
    pt_xml = re.sub(r'<pageFields\b.*?</pageFields>', lambda m: re.sub(r'\b(fld)="(\d+)"', shift_field, m.group(0)), pt_xml, count=1, flags=re.S)
    pt_xml = re.sub(r'<dataFields\b.*?</dataFields>', lambda m: re.sub(r'\b(fld)="(\d+)"', shift_field, m.group(0)), pt_xml, count=1, flags=re.S)
    n_df = int(re.search(r'<dataFields count="(\d+)"', pt_xml).group(1)) + 1
    pt_xml = re.sub(r'<dataFields count="\d+">(.*?)</dataFields>',
                    lambda m: f'<dataFields count="{n_df}">' + m.group(1) + f'<dataField name=" {escape(name)}" fld="{index}" baseField="0" baseItem="0"/></dataFields>',
                    pt_xml, count=1, flags=re.S)
    if "<colItems" in pt_xml:
        pt_xml = re.sub(r'<colItems count="(\d+)">(.*?)</colItems>',
                        lambda m: f'<colItems count="{int(m.group(1)) + 1}">' + m.group(2) + f'<i i="{n_df - 1}"><x v="{n_df - 1}"/></i></colItems>', pt_xml, count=1, flags=re.S)
    def widen(m):
        c1, r1, c2, r2 = m.group(1), m.group(2), m.group(3), m.group(4)
        return f'<location ref="{c1}{r1}:{col_letter(col_index(c2) + 1)}{r2}"'
    pt_xml = re.sub(r'<location ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', widen, pt_xml, count=1)
    return pt_xml, cd_xml


def reset_pivot_state(pt_xml):
    """Сохранённое состояние сводной второго кабинета — снять (сорок вторая §2): pageField без выбранного item («(Все)»), h="1" нигде (скрытых
    элементов нет), поля столбцов — sortType="ascending" (порядок колонок под формулы владельца). items полей осей, rowItems и colItems владельца ОСТАЮТСЯ: вариант «items → <item t="default"/>, rowItems / colItems сняты»
    Excel открывает, но на refresh падает (EXC_BAD_INSTRUCTION в mbukernel, 2026-09-27, маленькие книги Q2 / S2); с элементами владельца refresh
    проходит (S1 / S3). Ссылки items на чужие значения кэша снимает refresh с missingItemsLimit="0" на pivotCacheDefinition (см. transplant);
    поля данных не трогаются — видны все."""
    pt_xml = re.sub(r'(<pageField\b[^>]*?)\s+item="\d+"', r"\1", pt_xml)
    pt_xml = re.sub(r'\s+h="1"', "", pt_xml)
    pt_xml = re.sub(r'(<pivotTableDefinition\b[^>]*?)\s+missingItemsLimit="[^"]*"', r"\1", pt_xml, count=1)   # атрибут кэша, не сводной
    # поля столбцов — по алфавиту: без sortType Excel ставит статьи в порядке первого появления в источнике (большая книга 09-27: Эквайринг,
    # Логистика, Прочее, Товарооборот, Комиссия, Реклама), а формулы владельца справа ссылаются на колонки по алфавитному порядку
    # (P = J «Товарооборот / Начисления») — все 42 сравнения формул Ozon разошлись
    pt_xml = re.sub(r'<pivotField\b(?![^>]*\bsortType=)([^>]*\baxis="axisCol"[^>]*?)(/?>)', r'<pivotField\1 sortType="ascending"\2', pt_xml)
    return pt_xml


def clear_shared_items(cd_xml):
    """Чужие значения кэша второго кабинета (бренды, артикулы, SKU, наименования в sharedItems полей источника) — заменить заглушками «·1», «·2», …
    (сорок вторая §2). Структура sharedItems (число элементов, атрибуты типов, <m/> пустых) сохраняется: пустой <sharedItems/> при живых ссылках
    items сводной и при refreshOnLoad Excel считает повреждением (проверено на маленьких книгах: варианты Q3 / Q5 — диалог восстановления, сброс
    состояния без правки кэша — открывается). После refreshOnLoad и с missingItemsLimit="0" заглушки исчезают — остаются только наши элементы.
    Групповые и вычисляемые поля (databaseField="0") и groupItems не трогаются."""
    def field(m):
        f = m.group(0)
        if 'databaseField="0"' in f[:200]:
            return f
        n = [0]

        def s_item(mm):
            n[0] += 1
            return f'<s v="·{n[0]}"/>'

        def shared(ms):                                   # только внутри <sharedItems>: groupItems (подписи дней / месяцев) — Excel-евы, не чужие
            return re.sub(r'<s v="[^"]*"(?:\s+u="1")?/>', s_item, ms.group(0))
        return re.sub(r'<sharedItems\b[^>]*>.*?</sharedItems>', shared, f, flags=re.S)
    return re.sub(r'<cacheField\b[^>]*>.*?</cacheField>', field, cd_xml, flags=re.S)


# формулы владельца справа от сводной — по колонкам области сводной (сорок вторая §4); ссылки прямые, раскладка после сброса состояния полная:
# Ozon — A метки, B … M = 6 статей × (Начисления, Ст-ть) по алфавиту статей; WB — B … H = семь денег в порядке dataFields
FORMULAS = {
    "Сводная Ozon выкупы": {"first_row": 12, "columns": [
        ("P", 'IF(J{r}="","",J{r})'), ("Q", 'IF(B{r}="","",-B{r})'), ("R", 'IFERROR(IF(P{r}="","",Q{r}/P{r}),"")'), ("S", 'IF(P{r}="","",(P{r}-Q{r})/1.22)'),
        ("T", 'IF(K{r}="","",K{r})'), ("U", 'IF(S{r}="","",S{r}-T{r})'), ("V", 'IFERROR(IF(S{r}="","",U{r}/S{r}),"")'), ("W", 'IF(D{r}="","",-D{r})'),
        ("X", 'IFERROR(IF(S{r}="","",W{r}/S{r}),"")'), ("Y", 'IF(H{r}="","",-H{r})'), ("Z", 'IFERROR(IF(S{r}="","",Y{r}/S{r}),"")'), ("AA", 'IF(L{r}="","",-L{r})'),
        ("AB", 'IFERROR(IF(S{r}="","",AA{r}/S{r}),"")'), ("AC", 'IF(F{r}="","",-F{r})'), ("AD", 'IF(S{r}="","",U{r}-W{r}-Y{r}-AA{r}-AC{r})'),
        ("AE", 'IFERROR(IF(S{r}="","",AD{r}/S{r}),"")')]},
    "Сводная WB выкупы": {"first_row": 8, "columns": [
        ("K", 'IF(B{r}="","",B{r})'), ("L", 'IF(C{r}="","",C{r})'), ("M", 'IFERROR(IF(K{r}="","",L{r}/K{r}),"")'), ("N", 'IF(K{r}="","",(K{r}-L{r})/1.22)'),
        ("O", 'IF(D{r}="","",D{r})'), ("P", 'IF(N{r}="","",N{r}-O{r})'), ("Q", 'IFERROR(IF(N{r}="","",P{r}/N{r}),"")'), ("R", 'IF(N{r}="","",(F{r}+G{r})/1.22)'),
        ("S", 'IFERROR(IF(N{r}="","",R{r}/N{r}),"")'), ("T", 'IF(N{r}="","",E{r}/1.22)'), ("U", 'IFERROR(IF(N{r}="","",T{r}/N{r}),"")'),
        ("V", 'IF(N{r}="","",I{r}/1.22)'),                      # эквайринг — восьмое поле сводной (I, с НДС) без НДС, как R / T / X владельца (сорок четвёртая §2)
        ("W", 'IFERROR(IF(N{r}="","",V{r}/N{r}),"")'), ("X", 'IF(N{r}="","",H{r}/1.22)'), ("Y", 'IF(N{r}="","",P{r}-R{r}-T{r}-V{r}-X{r})'),
        ("Z", 'IFERROR(IF(N{r}="","",Y{r}/N{r}),"")')]},
}
FORMULA_ROWS = 320   # месяцы + все дни окна + итог: апрель … декабрь — 9 + 275 + 1 = 285, с запасом


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def write_formulas(sheet_xml, spec, rows=FORMULA_ROWS):
    """Формулы владельца в колонках spec на строках first_row … first_row + rows − 1 (сорок вторая §4): чужие формулы этих колонок сняты, каждая
    формула — с защитой от пустой строки и деления (пусто → ""), стиль ячейки — как у первой формульной строки владельца в той же колонке.
    Строки за пределами листа создаются; ячейки строки остаются в порядке колонок."""
    cols = [c for c, _f in spec["columns"]]
    r0, r1 = spec["first_row"], spec["first_row"] + rows - 1
    style_of = {}
    for c in cols:
        m = re.search(rf'<c r="{c}\d+"([^>]*)>\s*<f\b', sheet_xml) or re.search(rf'<c r="{c}{r0}"([^>]*?)/?>', sheet_xml)   # без формулы (V у WB — ввод) — стиль ячейки first_row
        if m and re.search(r's="(\d+)"', m.group(1)):
            style_of[c] = re.search(r's="(\d+)"', m.group(1)).group(1)
    colset = set(cols)

    def cell_col(tag):
        return re.match(r'<c r="([A-Z]+)\d+"', tag).group(1)

    def rebuild(r, row_xml):
        head = re.match(r'<row [^>]*?(/?)>', row_xml).group(0)
        body = row_xml[len(head):-len("</row>")] if not head.endswith("/>") else ""
        head = head[:-2] + ">" if head.endswith("/>") else head
        cells = re.findall(r'<c r="[A-Z]+\d+"[^>]*/>|<c r="[A-Z]+\d+"[^>]*>.*?</c>', body, re.S)
        # чужие формулы этих колонок снимаются на ВСЕХ строках (у владельца они общие — t="shared": без мастера в строке 12 зависимые ячейки ниже
        # r1 ломают книгу); текст и значения вне диапазона (шапка P11 … AE11) остаются
        keep = [c for c in cells if cell_col(c) not in colset or (not (r0 <= r <= r1) and "<f" not in c)]
        if r0 <= r <= r1:
            new = [f'<c r="{c}{r}"' + (f' s="{style_of[c]}"' if c in style_of else "") + f"><f>{escape(f.format(r=r))}</f></c>" for c, f in spec["columns"]]
            keep = sorted(keep + new, key=lambda c: col_index(cell_col(c)))
        return head + "".join(keep) + "</row>"
    # sheetData пересобирается целиком: существующие строки — по порядку, недостающие строки диапазона вставляются между ними
    m_sd = re.search(r"<sheetData>(.*?)</sheetData>|<sheetData\s*/>", sheet_xml, re.S)
    body = m_sd.group(1) or ""
    existing = [(int(m.group(1)), m.group(0)) for m in re.finditer(r'<row r="(\d+)"[^>]*(?:/>|>.*?</row>)', body, re.S)]
    have = {r for r, _x in existing}
    merged = sorted(existing + [(r, f'<row r="{r}"/>') for r in range(r0, r1 + 1) if r not in have], key=lambda t: t[0])
    sheet_xml = sheet_xml[:m_sd.start()] + "<sheetData>" + "".join(rebuild(r, x) for r, x in merged) + "</sheetData>" + sheet_xml[m_sd.end():]
    # dimension листа
    sheet_xml = re.sub(r'<dimension ref="([A-Z]+\d+):([A-Z]+)(\d+)"/>', lambda m: f'<dimension ref="{m.group(1)}:{m.group(2)}{max(int(m.group(3)), r1)}"/>', sheet_xml)
    return sheet_xml


ROW_TAG = b"<row "


def count_rows(z, part, chunk_size=8 << 20):
    """Число строк листа по XML (<row …>), потоково — лист может весить сотни МБ. Хвост между кусками — len(<row ) − 1 байт: с хвостом в 5 байт
    метка, попавшая ровно на границу куска, считалась дважды (книга 09-28: 533 564 при 533 563 строках, «проверка сводных: ошибок 1»)."""
    n, tail = 0, b""
    with z.open(part) as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            buf = tail + chunk
            n += buf.count(ROW_TAG)
            tail = buf[-(len(ROW_TAG) - 1):]
    return n


def transplant(book, out, specs=SPECS, row_counts=None, date_range=None):
    """Собирает out из book + сводных владельца. row_counts {лист: строк} — если известно из сборки; иначе считается по XML.
    date_range (d1, d2) — окно книги ISO: границы группировок по дате в rangePr (startDate / endDate); None — даты владельца как есть."""
    zin = zipfile.ZipFile(book)
    names = zin.namelist()
    sheets, wrels = workbook_sheets(zin)
    by_name = {s[0]: s for s in sheets}
    wb_xml = zin.read("xl/workbook.xml").decode("utf-8")
    rels_xml = zin.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    ct_xml = zin.read("[Content_Types].xml").decode("utf-8")
    styles = zin.read("xl/styles.xml").decode("utf-8")
    next_sheet_id = max(s[1] for s in sheets) + 1
    next_rid = max(int(re.sub(r"\D", "", r) or 0) for r in wrels) + 1
    next_sheet_no = max(int(m) for m in re.findall(r"xl/worksheets/sheet(\d+)\.xml", "\n".join(names))) + 1
    new_parts, new_sheets, new_rels, new_ct, pivot_caches, dnames, slicer_ext = {}, [], [], [], [], [], []
    slicer_no = 1
    report = []
    for k, spec in enumerate(specs, 1):
        opath = os.path.join(ROOT, spec["owner"])
        oz = zipfile.ZipFile(opath)
        parts = owner_parts(oz, spec["owner_sheet"])
        src = by_name.get(spec["source_sheet"])
        if src is None:
            raise SystemExit(f"в книге нет листа-источника «{spec['source_sheet']}»")
        src_part = wrels[src[2]][1]
        nrows = (row_counts or {}).get(spec["source_sheet"]) or count_rows(zin, src_part)
        ref = f"A1:{col_letter(spec['ncols'])}{nrows}"
        sheet_id, sheet_no = next_sheet_id, next_sheet_no
        next_sheet_id += 1; next_sheet_no += 1
        styles, dxf_offset, numfmt_map, xf_map = merge_styles(styles, oz.read("xl/styles.xml").decode("utf-8"))
        # pivotTable: форматы — в наши индексы; сохранённое состояние (фильтры, скрытые элементы, раскладка) — сброшено
        pt_xml = reset_pivot_state(remap_formats(oz.read(parts["pivot"]).decode("utf-8"), dxf_offset, numfmt_map))
        cache_id = re.search(r'<pivotTableDefinition[^>]*\scacheId="(\d+)"', pt_xml).group(1)
        pt_part = f"xl/pivotTables/pivotTable{k}.xml"
        new_parts[f"xl/pivotTables/_rels/pivotTable{k}.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{NS_PKG}"><Relationship Id="rId1" '
            f'Type="{REL}pivotCacheDefinition" Target="../pivotCache/pivotCacheDefinition{k}.xml"/></Relationships>').encode()
        # cache definition → worksheet source
        cd_xml = oz.read(parts["cache"]).decode("utf-8")
        cd_xml = re.sub(r'<cacheSource[^>]*/>|<cacheSource[^>]*>.*?</cacheSource>',
                        f'<cacheSource type="worksheet"><worksheetSource ref="{ref}" sheet="{escape(spec["source_sheet"])}"/></cacheSource>', cd_xml, count=1, flags=re.S)
        cd_xml = re.sub(r'\s+recordCount="\d+"', ' recordCount="0"', cd_xml, count=1)
        if spec.get("extra_field"):
            pt_xml, cd_xml = add_data_field(pt_xml, cd_xml, spec["extra_field"]["name"], spec["extra_field"]["index"])
        new_parts[pt_part] = pt_xml.encode("utf-8")
        src_fields, grp_fields = source_fields(cd_xml)
        if len(src_fields) != spec["ncols"]:
            raise SystemExit(f"{spec['owner']}: полей источника в кэше {len(src_fields)}, в SPECS {spec['ncols']} — {src_fields}")
        # группировка по дате: Excel открывает кэш ТОЛЬКО с startDate / endDate в rangePr — вариант 39-й «autoStart / autoEnd без дат» давал «Ошибка в части
        # содержимого» и потерю всех сводных (сорок первая §2). Границы — окно книги; groupItems владельца не трогаем, Excel перестраивает группы при refreshOnLoad
        if date_range:
            d1, d2 = date_range
            cd_xml = re.sub(r'<rangePr groupBy="([^"]+)"[^/]*/>', lambda m: f'<rangePr groupBy="{m.group(1)}" startDate="{d1}T00:00:00" endDate="{d2}T00:00:00"/>', cd_xml)
        if 'refreshOnLoad="1"' not in cd_xml:
            cd_xml = cd_xml.replace("<pivotCacheDefinition ", '<pivotCacheDefinition refreshOnLoad="1" ', 1)
        # элементы без записей (заглушки чужих значений) не хранить: атрибут pivotCacheDefinition по схеме OOXML; на pivotTableDefinition Excel его
        # молча игнорирует — так было в первой сборке 42-й, и чужие бренды переживали два refresh (проверено AppleScript: missing items limit = missing value)
        cd_xml = re.sub(r'\s+missingItemsLimit="[^"]*"', "", cd_xml, count=1)
        cd_xml = cd_xml.replace("<pivotCacheDefinition ", '<pivotCacheDefinition missingItemsLimit="0" ', 1)
        cd_xml = re.sub(r'numFmtId="(\d+)"', lambda m: f'numFmtId="{numfmt_map.get(int(m.group(1)), int(m.group(1)))}"', cd_xml)
        cd_xml = clear_shared_items(cd_xml)
        new_parts[f"xl/pivotCache/pivotCacheDefinition{k}.xml"] = cd_xml.encode("utf-8")
        new_parts[f"xl/pivotCache/_rels/pivotCacheDefinition{k}.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{NS_PKG}"><Relationship Id="rId1" '
            f'Type="{REL}pivotCacheRecords" Target="pivotCacheRecords{k}.xml"/></Relationships>').encode()
        new_parts[f"xl/pivotCache/pivotCacheRecords{k}.xml"] = EMPTY_RECORDS
        # sheet
        sh_xml = restyle_sheet(inline_strings(oz.read(parts["sheet"]).decode("utf-8"), parts["strings"]), xf_map)
        if spec["new_sheet"] in FORMULAS:
            sh_xml = write_formulas(sh_xml, FORMULAS[spec["new_sheet"]])
        sheet_part = f"xl/worksheets/sheet{sheet_no}.xml"
        rels = [f'<Relationship Id="rId1" Type="{REL}pivotTable" Target="../pivotTables/pivotTable{k}.xml"/>']
        orels = rels_of(oz, parts["sheet"])
        rid_pt = [r for r, (t, _p, _m) in orels.items() if t == REL + "pivotTable"][0]
        sh_xml = sh_xml.replace(f'r:id="{rid_pt}"', 'r:id="rId1"')   # ссылок на pivotTable в XML листа нет, но на всякий случай
        if parts["drawing"]:
            dpart = f"xl/drawings/drawing{k}.xml"
            new_parts[dpart] = oz.read(parts["drawing"])
            rid_dr = [r for r, (t, _p, _m) in orels.items() if t == REL + "drawing"][0]
            sh_xml = re.sub(r'<drawing r:id="[^"]+"/>', '<drawing r:id="rId2"/>', sh_xml)
            rels.append(f'<Relationship Id="rId2" Type="{REL}drawing" Target="../drawings/drawing{k}.xml"/>')
            new_ct.append((f"/{dpart}", CT["drawing"]))
        for sp in parts["slicers"]:
            spart = f"xl/slicers/slicer{slicer_no}.xml"
            new_parts[spart] = oz.read(sp)
            rid_sl = [r for r, (t, p, _m) in orels.items() if p == sp][0]
            sh_xml = sh_xml.replace(f'<x14:slicer r:id="{rid_sl}"/>', '<x14:slicer r:id="rId3"/>')
            rels.append(f'<Relationship Id="rId3" Type="{REL_MS}slicer" Target="../slicers/slicer{slicer_no}.xml"/>')
            new_ct.append((f"/{spart}", CT["slicer"]))
            slicer_no += 1
        for scp in parts["slicer_caches"]:
            n = len([c for c in new_ct if c[1] == CT["slicerCache"]]) + 1
            scpart = f"xl/slicerCaches/slicerCache{n}.xml"
            sc_xml = oz.read(scp).decode("utf-8")
            sc_xml = re.sub(r'<pivotTable tabId="\d+"', f'<pivotTable tabId="{sheet_id}"', sc_xml)
            new_parts[scpart] = sc_xml.encode("utf-8")
            rid = f"rId{next_rid}"; next_rid += 1
            new_rels.append(f'<Relationship Id="{rid}" Type="{REL_MS}slicerCache" Target="/{scpart}"/>')
            slicer_ext.append(f'<x14:slicerCache r:id="{rid}"/>')
            new_ct.append((f"/{scpart}", CT["slicerCache"]))
        dnames += parts["defined_names"]
        new_parts[sheet_part] = sh_xml.encode("utf-8")
        new_parts[f"xl/worksheets/_rels/sheet{sheet_no}.xml.rels"] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{NS_PKG}">' + "".join(rels) + "</Relationships>").encode()
        rid_sheet = f"rId{next_rid}"; next_rid += 1
        rid_cache = f"rId{next_rid}"; next_rid += 1
        new_sheets.append(f'<sheet name="{escape(spec["new_sheet"])}" sheetId="{sheet_id}" state="visible" r:id="{rid_sheet}"/>')
        new_rels.append(f'<Relationship Id="{rid_sheet}" Type="{REL}worksheet" Target="/{sheet_part}"/>')
        new_rels.append(f'<Relationship Id="{rid_cache}" Type="{REL}pivotCacheDefinition" Target="/xl/pivotCache/pivotCacheDefinition{k}.xml"/>')
        pivot_caches.append(f'<pivotCache cacheId="{cache_id}" r:id="{rid_cache}"/>')
        new_ct += [(f"/{sheet_part}", CT["worksheet"]), (f"/{pt_part}", CT["pivotTable"]),
                   (f"/xl/pivotCache/pivotCacheDefinition{k}.xml", CT["pivotCacheDefinition"]), (f"/xl/pivotCache/pivotCacheRecords{k}.xml", CT["pivotCacheRecords"])]
        report.append({"sheet": spec["new_sheet"], "source": spec["source_sheet"], "ref": ref, "rows": nrows, "cache_id": cache_id, "sheet_id": sheet_id,
                       "fields": len(re.findall(r'<cacheField ', cd_xml)), "source_fields": len(src_fields), "group_fields": grp_fields, "slicers": len(parts["slicers"]),
                       "dxf": len(re.findall(r'<dxf>', oz.read("xl/styles.xml").decode("utf-8"))), "xf": len(xf_map) - 1,
                       "shared_items_left": len(re.findall(r'<sharedItems\b[^/>]', cd_xml)), "formula_rows": FORMULA_ROWS if spec["new_sheet"] in FORMULAS else 0})
    # workbook.xml
    wb_xml = wb_xml.replace("</sheets>", "".join(new_sheets) + "</sheets>", 1)
    if dnames:
        wb_xml = re.sub(r'<definedNames\s*/>', "<definedNames>" + "".join(dnames) + "</definedNames>", wb_xml, count=1) if re.search(r'<definedNames\s*/>', wb_xml) \
            else wb_xml.replace("</definedNames>", "".join(dnames) + "</definedNames>", 1)
    tail = "<pivotCaches>" + "".join(pivot_caches) + "</pivotCaches>"
    if slicer_ext:
        tail += ('<extLst><ext uri="{BBE1A952-AA13-448e-AADC-164F8A28A991}" xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
                 "<x14:slicerCaches>" + "".join(slicer_ext) + "</x14:slicerCaches></ext></extLst>")
    wb_xml = re.sub(r'(<calcPr[^>]*/>)', r"\1" + tail, wb_xml, count=1)
    rels_xml = rels_xml.replace("</Relationships>", "".join(new_rels) + "</Relationships>", 1)
    ct_xml = ct_xml.replace("</Types>", "".join(f'<Override PartName="{p}" ContentType="{c}"/>' for p, c in new_ct) + "</Types>", 1)
    replaced = {"xl/workbook.xml": wb_xml.encode("utf-8"), "xl/_rels/workbook.xml.rels": rels_xml.encode("utf-8"), "[Content_Types].xml": ct_xml.encode("utf-8"),
                "xl/styles.xml": styles.encode("utf-8")}
    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
        for info in zin.infolist():
            if info.filename in replaced:
                zout.writestr(info.filename, replaced[info.filename])
            else:
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED            # без этого ZipInfo пишется как STORED: 81 МБ → 835 МБ
                with zin.open(info) as fin, zout.open(zi, "w", force_zip64=True) as fout:
                    shutil.copyfileobj(fin, fout, 8 << 20)
        for name, data in new_parts.items():
            zout.writestr(name, data)
    os.replace(tmp, out)
    return report


def check(book):
    """Структурная проверка книги со сводными; список ошибок (пусто — сошлось) и сводка."""
    errors, info = [], []
    z = zipfile.ZipFile(book)
    bad = z.testzip()
    if bad:
        errors.append(f"zip повреждён: {bad}")
    names = set(z.namelist())
    ct = z.read("[Content_Types].xml").decode("utf-8")
    overrides = set(re.findall(r'<Override PartName="([^"]+)"', ct))
    sheets, wrels = workbook_sheets(z)
    for name, sid, rid in sheets:
        if rid not in wrels or wrels[rid][1] not in names:
            errors.append(f"лист «{name}»: r:id {rid} не ведёт на часть")
    wb_xml = z.read("xl/workbook.xml").decode("utf-8")
    caches = dict(re.findall(r'<pivotCache cacheId="(\d+)" r:id="([^"]+)"', wb_xml))
    for part in sorted(n for n in names if n.startswith("xl/pivotTables/pivotTable") and n.endswith(".xml")):
        try:
            ET.fromstring(z.read(part))
        except ET.ParseError as exc:
            errors.append(f"{part}: XML не разбирается — {exc}"); continue
        pt = z.read(part).decode("utf-8")
        cid = re.search(r'\scacheId="(\d+)"', pt).group(1)
        if cid not in caches:
            errors.append(f"{part}: cacheId {cid} нет в workbook/pivotCaches")
        prels = rels_of(z, part)
        cdef = [p for t, p, _m in prels.values() if t == REL + "pivotCacheDefinition"]
        if not cdef or cdef[0] not in names:
            errors.append(f"{part}: pivotCacheDefinition не найдена"); continue
        cd = z.read(cdef[0]).decode("utf-8")
        ws = re.search(r'<worksheetSource ref="([^"]+)" sheet="([^"]+)"/>', cd)
        if not ws:
            errors.append(f"{cdef[0]}: нет worksheetSource"); continue
        ref, src_sheet = ws.group(1), ws.group(2).replace("&amp;", "&")
        fields, grp_fields = source_fields(cd)
        src = [s for s in sheets if s[0] == src_sheet]
        if not src:
            errors.append(f"{cdef[0]}: лист-источник «{src_sheet}» отсутствует"); continue
        src_part = wrels[src[0][2]][1]
        with z.open(src_part) as fh:
            head = fh.read(200000).decode("utf-8", "replace")
        first_row = re.search(r'<row [^>]*>(.*?)</row>', head, re.S)
        cells = re.findall(r'<c [^>]*>(.*?)</c>', first_row.group(1), re.S) if first_row else []
        headers = []
        for c in cells:
            t = re.search(r'<t[^>]*>(.*?)</t>', c, re.S)
            headers.append((t.group(1) if t else "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        ncols = int(re.match(r"A1:([A-Z]+)(\d+)", ref).group(2)) and len(fields)
        last_col = re.match(r"A1:([A-Z]+)(\d+)", ref).group(1)
        if headers[:len(fields)] != fields:
            diff = [(i, a, b) for i, (a, b) in enumerate(zip(headers[:len(fields)], fields), 1) if a != b]
            errors.append(f"{cdef[0]}: шапка «{src_sheet}» ≠ полям источника кэша: {diff[:5]}")
        inside = [h for h in headers[:len(fields)] if h in grp_fields]
        if inside:
            errors.append(f"{cdef[0]}: в ref сводной есть колонки с именами групповых / вычисляемых полей: {inside}")
        for rp in re.findall(r'<rangePr [^>]*/>', cd):
            if re.search(r'auto(Start|End)=', rp) or not (re.search(r'startDate="[^"]+"', rp) and re.search(r'endDate="[^"]+"', rp)):
                errors.append(f"{cdef[0]}: rangePr без startDate / endDate или с autoStart / autoEnd — Excel такой кэш не открывает (сорок первая §2): {rp}")
        if col_letter(len(fields)) != last_col:
            errors.append(f"{cdef[0]}: ref {ref} не по числу полей {len(fields)}")
        nrows = count_rows(z, src_part)
        if int(re.match(r"A1:([A-Z]+)(\d+)", ref).group(2)) != nrows:
            errors.append(f"{cdef[0]}: ref {ref}, а строк на листе {nrows}")
        if 'refreshOnLoad="1"' not in cd:
            errors.append(f"{cdef[0]}: нет refreshOnLoad")
        spec_ = next((sp for sp in SPECS if sp["source_sheet"] == src_sheet), None)
        if spec_ and spec_.get("data_fields"):
            n_df = int((re.search(r'<dataFields count="(\d+)"', pt) or re.search(r"()(0)", "0")).group(1))
            if n_df != spec_["data_fields"]:
                errors.append(f"{part}: полей данных {n_df}, ожидалось {spec_['data_fields']}")
            if spec_.get("extra_field"):
                n_ci = int((re.search(r'<colItems count="(\d+)"', pt) or re.search(r"()(0)", "0")).group(1))
                if n_ci != spec_["data_fields"]:
                    errors.append(f"{part}: colItems {n_ci}, ожидалось {spec_['data_fields']}")
                loc = re.search(r'<location ref="[A-Z]+\d+:([A-Z]+)\d+"', pt)
                if not loc or col_index(loc.group(1)) != spec_["data_fields"] + 1:
                    errors.append(f"{part}: location {loc.group(0) if loc else None} — ожидалась ширина {spec_['data_fields'] + 1} колонок")
        if not re.search(r'<pivotCacheDefinition\b[^>]*\smissingItemsLimit="0"', cd):
            errors.append(f"{cdef[0]}: нет missingItemsLimit=\"0\" на pivotCacheDefinition — чужие элементы переживут refresh")
        info.append(f"{part}: cacheId {cid}, источник «{src_sheet}» {ref}, полей источника {len(fields)} (+ не из источника {len(grp_fields)}: {grp_fields}), строк {nrows}")
    for part in sorted(n for n in names if n.startswith("xl/slicerCaches/")):
        sc = z.read(part).decode("utf-8")
        tab = re.search(r'<pivotTable tabId="(\d+)" name="([^"]+)"', sc)
        if tab and int(tab.group(1)) not in {s[1] for s in sheets}:
            errors.append(f"{part}: tabId {tab.group(1)} — такого sheetId нет")
        sc_name = re.search(r'slicerCacheDefinition[^>]*\sname="([^"]+)"', sc)
        info.append(f"{part}: {sc_name.group(1) if sc_name else '?'} → tabId {tab.group(1) if tab else '?'}")
    for part in sorted(n for n in names if n.startswith("xl/") and n.endswith(".xml") and ("pivot" in n or "slicer" in n or "drawings/" in n or "worksheets/sheet" in n)):
        if f"/{part}" not in overrides:
            errors.append(f"{part}: нет Override в [Content_Types]")
    for part in [n for n in names if n.startswith(("xl/pivot", "xl/slicer", "xl/drawings/", "xl/workbook.xml", "xl/styles.xml"))]:
        if part.endswith(".xml") or part.endswith(".rels"):
            try:
                ET.fromstring(z.read(part))
            except ET.ParseError as exc:
                errors.append(f"{part}: XML не разбирается — {exc}")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            import openpyxl
            wb = openpyxl.load_workbook(book, read_only=True)
            info.append(f"openpyxl read_only: листов {len(wb.sheetnames)}; предупреждений {len(w)}" + (f" ({str(w[0].message)[:80]})" if w else ""))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openpyxl не открывает: {type(exc).__name__}: {str(exc)[:160]}")
    return errors, info


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("book"); ap.add_argument("--out"); ap.add_argument("--check", action="store_true"); ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args(argv)
    if not args.check_only:
        out = args.out or args.book
        rep = transplant(args.book, out)
        for r in rep:
            print(f"лист «{r['sheet']}» ← «{r['source']}» {r['ref']} ({r['rows']} строк): cacheId {r['cache_id']}, sheetId {r['sheet_id']}, полей {r['fields']}, срезов {r['slicers']}, dxf владельца {r['dxf']}")
        print(f"книга → {out}: {os.path.getsize(out):,} байт")
        book = out
    else:
        book = args.book
    if args.check or args.check_only:
        errors, info = check(book)
        for line in info:
            print("  " + line)
        print("проверка: " + ("сошлось" if not errors else f"ошибок {len(errors)}"))
        for e in errors:
            print("  ✗ " + e)
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
