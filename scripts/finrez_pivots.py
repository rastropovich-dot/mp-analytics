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
    {"owner": "data/owner_finrez_ozon_buyouts_pivot.xlsx", "owner_sheet": "Вывод данных", "new_sheet": "Сводная Ozon выкупы", "source_sheet": "Данные Ozon выкупы", "ncols": 13},
    {"owner": "data/owner_finrez_wb_buyouts_pivot.xlsx", "owner_sheet": "Свод", "new_sheet": "Сводная WB выкупы", "source_sheet": "Данные WB выкупы", "ncols": 14},
    # заказы: 21 колонка источника (Дата … День); «Месяцы» — группировка, «Маржа …» … «Фин.рез %» (8) — ВЫЧИСЛЯЕМЫЕ поля сводной (formula в кэше),
    # Excel считает их сам из полей источника по именам; в нашем листе одноимённые колонки остаются справа от ref справочно
    {"owner": "data/owner_finrez_orders_pivot.xlsx", "owner_sheet": "Свод", "new_sheet": "Сводная заказы", "source_sheet": "Данные заказы", "ncols": 21},
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
    """t="s" → inlineStr по таблице строк владельца; стили ячеек и колонок сняты (индексы чужого styles.xml)."""
    def repl(m):
        attrs, idx = m.group(1), int(m.group(2))
        attrs = re.sub(r'\s+t="s"', "", attrs)
        text = strings[idx] if idx < len(strings) else ""
        return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    xml = re.sub(r'<c((?:\s+[a-zA-Z:]+="[^"]*")*?\s+t="s"(?:\s+[a-zA-Z:]+="[^"]*")*)><v>(\d+)</v></c>', repl, xml)
    xml = re.sub(r'\s+s="\d+"', "", xml)
    xml = re.sub(r'\s+style="\d+"', "", xml)
    xml = re.sub(r'<legacyDrawing [^>]*/>', "", xml)
    xml = re.sub(r'<pageSetup [^>]*/>', "", xml)
    xml = re.sub(r'\s+tabSelected="1"', "", xml)
    return xml


def remap_formats(xml, dxf_offset, numfmt_map):
    xml = re.sub(r'dxfId="(\d+)"', lambda m: f'dxfId="{int(m.group(1)) + dxf_offset}"', xml)
    xml = re.sub(r'numFmtId="(\d+)"', lambda m: f'numFmtId="{numfmt_map.get(int(m.group(1)), int(m.group(1)))}"', xml)
    return xml


def merge_styles(our_styles, owner_styles):
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


def count_rows(z, part):
    """Число строк листа по XML (<row …>), потоково — лист может весить сотни МБ."""
    n, tail = 0, b""
    with z.open(part) as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            buf = tail + chunk
            n += buf.count(b"<row ")
            tail = buf[-5:]
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
        styles, dxf_offset, numfmt_map = merge_styles(styles, oz.read("xl/styles.xml").decode("utf-8"))
        # pivotTable
        pt_xml = remap_formats(oz.read(parts["pivot"]).decode("utf-8"), dxf_offset, numfmt_map)
        cache_id = re.search(r'<pivotTableDefinition[^>]*\scacheId="(\d+)"', pt_xml).group(1)
        pt_part = f"xl/pivotTables/pivotTable{k}.xml"
        new_parts[pt_part] = pt_xml.encode("utf-8")
        new_parts[f"xl/pivotTables/_rels/pivotTable{k}.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{NS_PKG}"><Relationship Id="rId1" '
            f'Type="{REL}pivotCacheDefinition" Target="../pivotCache/pivotCacheDefinition{k}.xml"/></Relationships>').encode()
        # cache definition → worksheet source
        cd_xml = oz.read(parts["cache"]).decode("utf-8")
        cd_xml = re.sub(r'<cacheSource[^>]*/>|<cacheSource[^>]*>.*?</cacheSource>',
                        f'<cacheSource type="worksheet"><worksheetSource ref="{ref}" sheet="{escape(spec["source_sheet"])}"/></cacheSource>', cd_xml, count=1, flags=re.S)
        cd_xml = re.sub(r'\s+recordCount="\d+"', ' recordCount="0"', cd_xml, count=1)
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
        cd_xml = re.sub(r'numFmtId="(\d+)"', lambda m: f'numFmtId="{numfmt_map.get(int(m.group(1)), int(m.group(1)))}"', cd_xml)
        new_parts[f"xl/pivotCache/pivotCacheDefinition{k}.xml"] = cd_xml.encode("utf-8")
        new_parts[f"xl/pivotCache/_rels/pivotCacheDefinition{k}.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{NS_PKG}"><Relationship Id="rId1" '
            f'Type="{REL}pivotCacheRecords" Target="pivotCacheRecords{k}.xml"/></Relationships>').encode()
        new_parts[f"xl/pivotCache/pivotCacheRecords{k}.xml"] = EMPTY_RECORDS
        # sheet
        sh_xml = inline_strings(oz.read(parts["sheet"]).decode("utf-8"), parts["strings"])
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
                       "dxf": len(re.findall(r'<dxf>', oz.read("xl/styles.xml").decode("utf-8")))})
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
