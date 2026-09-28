"""Тридцать восьмая §4: пересадка сводных владельца — чистые преобразования XML; интеграция — только если оригиналы владельца лежат в data/ (вне git)."""
import os
import sys
import re
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import finrez_pivots as fp  # noqa: E402


class Transforms(unittest.TestCase):
    def test_merge_styles_full_renumbers_everything(self):
        ours = ('<styleSheet><numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%"/></numFmts><fonts count="2"><font><b/></font><font/></fonts>'
                '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><dxfs count="0"/><tableStyles count="0"/></styleSheet>')
        owner = ('<styleSheet><numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.00"/><numFmt numFmtId="165" formatCode="0.0%"/></numFmts>'
                 '<fonts count="2" x14ac:knownFonts="1"><font><sz val="11"/></font><font><b/><color rgb="FFFFFFFF"/></font></fonts>'
                 '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill></fills>'
                 '<borders count="2"><border/><border><left style="thin"/></border></borders>'
                 '<cellStyleXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="43" fontId="1" fillId="0" borderId="0"/><xf numFmtId="9" fontId="1" fillId="0" borderId="0"/></cellStyleXfs>'
                 '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1"><alignment horizontal="center"/></xf>'
                 '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="2" applyNumberFormat="1"/></cellXfs>'
                 '<cellStyles count="3"><cellStyle name="Обычный" xfId="0" builtinId="0"/><cellStyle name="Процентный" xfId="2" builtinId="5"/><cellStyle name="Финансовый" xfId="1" builtinId="3"/></cellStyles>'
                 '<dxfs count="1"><dxf><numFmt numFmtId="165" formatCode="0.0%"/></dxf></dxfs></styleSheet>')
        merged, dxf_off, fmap, xf_map = fp.merge_styles(ours, owner)
        self.assertEqual((dxf_off, fmap, xf_map), (0, {164: 165, 165: 166}, {0: 0, 1: 2, 2: 3}))                   # numFmt 164 владельца ≠ наш 164 → новые id
        self.assertIn('<fonts count="4">', merged); self.assertIn('<fills count="5">', merged); self.assertIn('<borders count="3">', merged)
        self.assertIn('<cellStyleXfs count="3">', merged); self.assertIn('<cellXfs count="4">', merged)
        self.assertIn('<xf numFmtId="0" fontId="3" fillId="4" borderId="2" xfId="0" applyFont="1"><alignment horizontal="center"/></xf>', merged)   # шрифт +2, заливка +2, граница +1
        self.assertIn('<xf numFmtId="166" fontId="2" fillId="2" borderId="1" xfId="2" applyNumberFormat="1"/>', merged)                              # xfId 2 → наш cellStyleXfs 2
        self.assertIn('<cellStyle name="Процентный" xfId="2" builtinId="5"/>', merged); self.assertIn('<cellStyle name="Финансовый" xfId="1" builtinId="3"/>', merged)
        self.assertNotIn('name="Обычный"', merged)
        self.assertIn('<dxfs count="1"><dxf><numFmt numFmtId="166"', merged)

    def test_reset_pivot_state(self):
        pt = ('<pivotTableDefinition name="p" cacheId="1"><location ref="A1:C9"/><pivotFields count="3"><pivotField axis="axisRow" showAll="0"><items count="3">'
              '<item x="0"/><item x="1" h="1"/><item t="default"/></items></pivotField><pivotField dataField="1" showAll="0"/><pivotField axis="axisPage" showAll="0">'
              '<items count="2"><item x="0"/><item t="default"/></items></pivotField></pivotFields><rowFields count="1"><field x="0"/></rowFields>'
              '<rowItems count="2"><i><x/></i><i t="grand"><x/></i></rowItems><colItems count="1"><i/></colItems><pageFields count="1"><pageField fld="2" hier="-1" item="0"/></pageFields>'
              '<dataFields count="1"><dataField name="Σ" fld="1"/></dataFields></pivotTableDefinition>')
        out = fp.reset_pivot_state('<pivotTableDefinition missingItemsLimit="0" name="p"' + pt[len('<pivotTableDefinition name="p"'):])
        self.assertTrue(out.startswith('<pivotTableDefinition name="p" cacheId="1">'))                  # атрибут кэша со сводной снят
        self.assertNotIn('h="1"', out); self.assertIn('<item x="1"/>', out)                                # скрытых нет, элементы владельца целы
        self.assertIn('<items count="3"><item x="0"/><item x="1"/><item t="default"/></items>', out)     # items не сброшены: сброс валит Excel на refresh
        self.assertIn('<rowItems count="2"><i><x/></i><i t="grand"><x/></i></rowItems><colItems count="1"><i/></colItems>', out)
        self.assertIn('<pageField fld="2" hier="-1"/>', out)
        col = fp.reset_pivot_state('<pivotTableDefinition name="p"><pivotFields count="2"><pivotField axis="axisCol" showAll="0"><items count="1"><item t="default"/></items></pivotField>'
                                   '<pivotField axis="axisCol" sortType="descending" showAll="0"/></pivotFields></pivotTableDefinition>')
        self.assertIn('<pivotField axis="axisCol" showAll="0" sortType="ascending">', col)          # поле столбцов без сортировки — по алфавиту
        self.assertIn('<pivotField axis="axisCol" sortType="descending" showAll="0"/>', col)         # явная сортировка владельца не трогается
        self.assertIn('<dataFields count="1"><dataField name="Σ" fld="1"/></dataFields>', out)

    def test_clear_shared_items_keeps_group_and_calculated_fields(self):
        cd = ('<pivotCacheDefinition><cacheFields count="3"><cacheField name="Бренд" numFmtId="0"><sharedItems count="2"><s v="Pastel"/><s v="KARATOV"/></sharedItems></cacheField>'
              '<cacheField name="Дата" numFmtId="0"><sharedItems containsDate="1" count="1"><d v="2026-04-01T00:00:00"/></sharedItems><fieldGroup par="2" base="1"><rangePr groupBy="days"/><groupItems count="1"><s v="01.апр"/></groupItems></fieldGroup></cacheField>'
              '<cacheField name="Месяцы" numFmtId="0" databaseField="0"><fieldGroup base="1"><rangePr groupBy="months"/><groupItems count="1"><s v="апр"/></groupItems></fieldGroup></cacheField></cacheFields></pivotCacheDefinition>')
        out = fp.clear_shared_items(cd)
        self.assertNotIn("Pastel", out); self.assertNotIn("KARATOV", out)
        self.assertIn('<cacheField name="Бренд" numFmtId="0"><sharedItems count="2"><s v="·1"/><s v="·2"/></sharedItems></cacheField>', out)   # структура цела, значения — заглушки
        self.assertIn('<d v="2026-04-01T00:00:00"/>', out)                                                                                # даты и числа — не чужие, остаются
        self.assertIn('<groupItems count="1"><s v="01.апр"/></groupItems>', out); self.assertIn('<s v="апр"/>', out)

    def test_write_formulas_full_range_guarded(self):
        spec = {"first_row": 3, "columns": [("P", 'IF(J{r}="","",J{r})'), ("R", 'IFERROR(IF(P{r}="","",Q{r}/P{r}),"")')]}
        xml = ('<worksheet><dimension ref="A1:R5"/><sheetData><row r="2"><c r="A2" t="inlineStr"><is><t>шапка</t></is></c><c r="P2" s="3" t="inlineStr"><is><t>Оборот</t></is></c></row>'
               '<row r="3"><c r="A3"><v>1</v></c><c r="J3"><v>5</v></c><c r="P3" s="9"><f t="shared" ref="P3:P5" si="0">J3</f><v>5</v></c><c r="R3" s="8"><f>Q3/P3</f><v>0.4</v></c></row>'
               '<row r="5"><c r="P5" s="9"><f t="shared" si="0"/><v>0</v></c><c r="Q5"><v>2</v></c></row></sheetData></worksheet>')
        out = fp.write_formulas(xml, spec, rows=4)
        self.assertIn('<c r="P2" s="3" t="inlineStr"><is><t>Оборот</t></is></c>', out)                       # шапка цела
        self.assertIn('<c r="A3"><v>1</v></c><c r="J3"><v>5</v></c><c r="P3" s="9"><f>IF(J3="","",J3)</f></c><c r="R3" s="8"><f>IFERROR(IF(P3="","",Q3/P3),"")</f></c></row>', out)
        self.assertIn('<row r="4"><c r="P4" s="9"><f>IF(J4="","",J4)</f></c><c r="R4" s="8"><f>IFERROR(IF(P4="","",Q4/P4),"")</f></c></row>', out)   # недостающая строка создана
        self.assertIn('<row r="5"><c r="P5" s="9"><f>IF(J5="","",J5)</f></c><c r="Q5"><v>2</v></c><c r="R5" s="8">', out)                              # порядок колонок
        self.assertIn('<row r="6"><c r="P6" s="9">', out)
        self.assertNotIn('t="shared"', out); self.assertIn('<dimension ref="A1:R6"/>', out)

    def test_inline_strings_keep_styles_drop_notes(self):
        xml = ('<worksheet><cols><col min="2" max="2" width="22" style="7"/></cols><sheetData><row r="3"><c r="A3" s="5" t="s"><v>1</v></c>'
               '<c r="B3" t="s"><v>0</v></c><c r="C3" s="2"><v>12.5</v></c></row></sheetData><legacyDrawing r:id="rId2"/><pageSetup paperSize="9" r:id="rId9"/></worksheet>')
        out = fp.inline_strings(xml, ["Статус", "Бренд & Ко"])
        self.assertIn('<c r="A3" s="5" t="inlineStr"><is><t xml:space="preserve">Бренд &amp; Ко</t></is></c>', out)      # стили остаются (сорок вторая §3)
        self.assertIn('<c r="B3" t="inlineStr"><is><t xml:space="preserve">Статус</t></is></c>', out)
        self.assertIn('<c r="C3" s="2"><v>12.5</v></c>', out)
        self.assertIn('style="7"', out); self.assertNotIn("legacyDrawing", out); self.assertNotIn("pageSetup", out)
        out = fp.restyle_sheet(out, {0: 0, 2: 12, 5: 15, 7: 17})
        self.assertIn('<c r="A3" s="15" t="inlineStr">', out); self.assertIn('<c r="C3" s="12">', out); self.assertIn('style="17"', out)

    def test_merge_styles_and_remap(self):
        ours = '<styleSheet><numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%"/></numFmts><fonts count="1"><font/></fonts><cellXfs count="1"><xf/></cellXfs><tableStyles count="0"/></styleSheet>'
        owner = ('<styleSheet><numFmts count="2"><numFmt numFmtId="3" formatCode="#,##0"/><numFmt numFmtId="165" formatCode="0.0%"/></numFmts>'
                 '<dxfs count="2"><dxf><numFmt numFmtId="165" formatCode="0.0%"/></dxf><dxf><fill/></dxf></dxfs></styleSheet>')
        merged, offset, fmap = fp._merge_numfmts_dxfs(ours, owner)
        self.assertEqual((offset, fmap), (0, {165: 165}))
        self.assertIn('<numFmts count="2"><numFmt numFmtId="164" formatCode="0.0%"/><numFmt numFmtId="165" formatCode="0.0%"/></numFmts>', merged)
        self.assertIn('<dxfs count="2"><dxf><numFmt numFmtId="165"', merged)
        merged2, offset2, fmap2 = fp._merge_numfmts_dxfs(merged, owner)          # второй файл владельца: свои dxf — со сдвигом, numFmt 165 → 166
        self.assertEqual((offset2, fmap2), (2, {165: 166}))
        self.assertIn('<dxfs count="4">', merged2)
        pt = fp.remap_formats('<format dxfId="1"/><dataField numFmtId="165"/><dataField numFmtId="3"/>', offset2, fmap2)
        self.assertEqual(pt, '<format dxfId="3"/><dataField numFmtId="166"/><dataField numFmtId="3"/>')

    def test_source_fields_exclude_groups(self):
        cd = ('<c><cacheField name="Дата" numFmtId="0"><fieldGroup par="2" base="0"><rangePr groupBy="days" startDate="2026-04-01T00:00:00" endDate="2026-09-21T00:00:00"/></fieldGroup></cacheField>'
              '<cacheField name="МП" numFmtId="0"><sharedItems/></cacheField><cacheField name="Месяцы" numFmtId="0" databaseField="0"><fieldGroup base="0"><rangePr groupBy="months"/></fieldGroup></cacheField></c>')
        self.assertEqual(fp.source_fields(cd), (["Дата", "МП"], ["Месяцы"]))     # «Дата» с fieldGroup — колонка источника; «Месяцы» (databaseField=0) — нет

    def test_col_letter(self):
        self.assertEqual([fp.col_letter(n) for n in (1, 15, 26, 27, 30)], ["A", "O", "Z", "AA", "AD"])


@unittest.skipUnless(all(os.path.exists(os.path.join(ROOT, s["owner"])) for s in fp.SPECS), "оригиналы владельца лежат вне git")
class Integration(unittest.TestCase):
    def test_transplant_into_a_small_book_and_check(self):
        import tempfile
        from decimal import Decimal as D
        import report_finrez as fr
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "book.xlsx")
            wb = openpyxl.Workbook(write_only=True)
            for title, cols, row in (("Данные Ozon выкупы", fr.LONG_COLS, [1, "2026-09-01", "F1", "11", 1, "Товарооборот", "KARATOV", "сен", 0, "KARATOV", "кольца", 100, "Кольцо", 1, 9, 0, "Ozon"]),
                                     ("Данные WB выкупы", [(h, k, None) for h, k, f in fr.wb_data_cols()], [None] * len(fr.wb_data_cols())),
                                     ("Данные заказы", fr.ORDER_DATA_COLS, [None] * len(fr.ORDER_DATA_COLS))):
                ws = wb.create_sheet(title); ws.append([h for h, _k, _f in cols]); ws.append(row)
            wb.save(path)
            rep = fp.transplant(path, path, row_counts={"Данные Ozon выкупы": 2, "Данные WB выкупы": 2, "Данные заказы": 2}, date_range=("2026-04-01", "2026-09-25"))
            self.assertEqual([r["ref"] for r in rep], ["A1:M2", "A1:O2", "A1:U2"])           # колонки источника: 13 / 15 (+ «Эквайринг, ₽») / 21
            pt2 = zipfile.ZipFile(path).read("xl/pivotTables/pivotTable2.xml").decode("utf-8"); cd2 = zipfile.ZipFile(path).read("xl/pivotCache/pivotCacheDefinition2.xml").decode("utf-8")
            self.assertIn('<dataFields count="8">', pt2); self.assertIn('<dataField name=" Эквайринг, ₽" fld="14" baseField="0" baseItem="0"/></dataFields>', pt2)
            self.assertIn('<colItems count="8">', pt2); self.assertIn('<i i="7"><x v="7"/></i></colItems>', pt2)
            self.assertIn('<location ref="A7:I', pt2); self.assertIn('<rowFields count="2"><field x="15"/><field x="0"/></rowFields>', pt2)
            self.assertEqual(re.findall(r'<cacheField name="([^"]+)"', cd2)[13:16], ["Статус", "Эквайринг, ₽", "Месяцы"]); self.assertIn('<fieldGroup par="15" base="0">', cd2)
            cd3 = zipfile.ZipFile(path).read("xl/pivotCache/pivotCacheDefinition3.xml").decode("utf-8")
            self.assertEqual(re.findall(r'<rangePr[^/]*/>', cd3),                                  # сорок первая §2: даты окна, без autoStart / autoEnd
                             ['<rangePr groupBy="days" startDate="2026-04-01T00:00:00" endDate="2026-09-25T00:00:00"/>',
                              '<rangePr groupBy="months" startDate="2026-04-01T00:00:00" endDate="2026-09-25T00:00:00"/>'])
            self.assertEqual([r["group_fields"] for r in rep][:2], [["Дни (Дата начисления)", "Месяцы (Дата начисления)"], ["Месяцы"]])
            self.assertEqual(rep[2]["group_fields"], ["Месяцы", "Маржа, руб без НДС", "Маржинальность, %", "ДДР, %", "Соинвест, %", "Комиссия сред", "Фин.рез", "Цена", "Фин.рез %"])
            errors, info = fp.check(path)
            self.assertEqual(errors, [], errors)
            self.assertIn('<pivotCacheDefinition missingItemsLimit="0" refreshOnLoad="1"', cd3)                       # лимит — на кэше, не на сводной
            self.assertNotIn("missingItemsLimit", zipfile.ZipFile(path).read("xl/pivotTables/pivotTable3.xml").decode("utf-8"))
            self.assertEqual(openpyxl.load_workbook(path, read_only=True).sheetnames[-3:], ["Сводная Ozon выкупы", "Сводная WB выкупы", "Сводная заказы"])

    def test_add_data_field_renumbers_fields_and_widens_location(self):
        pt = ('<pivotTableDefinition name="p"><location ref="A7:H96" firstHeaderRow="0"/><pivotFields count="4"><pivotField axis="axisRow"/><pivotField dataField="1"/>'
              '<pivotField axis="axisPage"/><pivotField axis="axisRow"><items count="1"><item x="0"/></items></pivotField></pivotFields>'
              '<rowFields count="2"><field x="3"/><field x="0"/></rowFields><colFields count="1"><field x="-2"/></colFields>'
              '<rowItems count="1"><i><x v="3"/></i></rowItems><colItems count="1"><i><x/></i></colItems><pageFields count="1"><pageField fld="2" hier="-1"/></pageFields>'
              '<dataFields count="1"><dataField name=" Продажи, ₽" fld="1" baseField="0" baseItem="0"/></dataFields></pivotTableDefinition>')
        cd = ('<pivotCacheDefinition><cacheFields count="4"><cacheField name="Дата"><sharedItems containsDate="1"><d v="2026-04-01T00:00:00"/></sharedItems><fieldGroup par="3" base="0"/></cacheField>'
              '<cacheField name="Продажи, ₽"><sharedItems containsNumber="1"/></cacheField><cacheField name="Статус"><sharedItems><s v="a"/></sharedItems></cacheField>'
              '<cacheField name="Месяцы" databaseField="0"><fieldGroup base="0"><groupItems count="1"><s v="апр"/></groupItems></fieldGroup></cacheField></cacheFields></pivotCacheDefinition>')
        pt2, cd2 = fp.add_data_field(pt, cd, "Эквайринг, ₽", 3)
        self.assertIn('<cacheFields count="5">', cd2); self.assertEqual(re.findall(r'<cacheField name="([^"]+)"', cd2), ["Дата", "Продажи, ₽", "Статус", "Эквайринг, ₽", "Месяцы"])
        self.assertIn('<fieldGroup par="4" base="0"/>', cd2)                                             # ссылка на группировку сдвинута
        self.assertIn('<pivotFields count="5">', pt2); self.assertIn('<pivotField axis="axisPage"/><pivotField dataField="1" showAll="0"/><pivotField axis="axisRow">', pt2)
        self.assertIn('<rowFields count="2"><field x="4"/><field x="0"/></rowFields>', pt2); self.assertIn('<field x="-2"/>', pt2)
        self.assertIn('<rowItems count="1"><i><x v="3"/></i></rowItems>', pt2)                            # номера ЭЛЕМЕНТОВ не трогаются
        self.assertIn('<pageField fld="2" hier="-1"/>', pt2)
        self.assertIn('<dataFields count="2"><dataField name=" Продажи, ₽" fld="1" baseField="0" baseItem="0"/><dataField name=" Эквайринг, ₽" fld="3" baseField="0" baseItem="0"/></dataFields>', pt2)
        self.assertIn('<colItems count="2"><i><x/></i><i i="1"><x v="1"/></i></colItems>', pt2); self.assertIn('<location ref="A7:I96"', pt2)
        with self.assertRaises(SystemExit):
            fp.add_data_field(pt2, cd2, "Эквайринг, ₽", 3)

    def test_count_rows_does_not_double_count_at_chunk_boundary(self):
        import io, zipfile
        xml = b'<sheetData>' + b''.join(b'<row r="%d"><c r="A%d"><v>1</v></c></row>' % (i, i) for i in range(1, 101)) + b'</sheetData>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("s.xml", xml)
        z = zipfile.ZipFile(buf)
        for size in range(1, 60):                                    # любой размер куска, включая границы ровно по «<row »
            self.assertEqual(fp.count_rows(z, "s.xml", chunk_size=size), 100, size)
        idx = xml.index(b'<row r="7"') + len(b"<row ")
        self.assertEqual(fp.count_rows(z, "s.xml", chunk_size=idx), 100)  # первый кусок кончается ровно на «<row »

if __name__ == "__main__":
    unittest.main()
