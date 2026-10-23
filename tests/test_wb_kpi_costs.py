"""Двадцатая WB: хранение, реклама и «прочее» по артикулу у WB — в витрину daily_sku_kpi (слово владельца 2026-10-23: А1 / Б / В)."""
import unittest
from decimal import Decimal
from unittest import mock

import reports_daily_sku_kpi as kpi
from loaders import wb_ads_allocation as alloc
from loaders import wb_money_rules as rules

EMPTY = {"storage": [], "ads": {}, "other": [], "articles": {}}


def build(wb_costs, orders=(), buyouts=(), expenses=(), placement=()):
    with mock.patch.object(kpi, "load_orders", return_value=list(orders)), mock.patch.object(kpi, "load_buyouts", return_value=list(buyouts)), \
         mock.patch.object(kpi, "load_expenses", return_value=list(expenses)), mock.patch.object(kpi, "load_ozon_organic", return_value=[]), \
         mock.patch.object(kpi, "load_placement", return_value=list(placement)), mock.patch.object(kpi, "load_wb_costs", return_value=wb_costs), \
         mock.patch.object(kpi, "load_wb_cohort", return_value=({}, {})), mock.patch("builtins.print"):
        return {(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]): r for r in kpi.build_kpi()}


class AllocationTests(unittest.TestCase):
    """Функции разнесения — те же, что были в scripts (книга и лист импортируют их обратно)."""

    UPD = [{"upd_day": "2026-07-01", "advert_id": 1, "upd_sum": "1000", "payment_type": "Баланс"},
           {"upd_day": "2026-07-01", "advert_id": 2, "upd_sum": "300", "payment_type": "Баланс"},
           {"upd_day": "2026-07-01", "advert_id": 2, "upd_sum": "700", "payment_type": "Кэшбэк"},
           {"upd_day": "2026-07-02", "advert_id": 1, "upd_sum": "500", "payment_type": "Баланс"}]
    NM = [{"day": "2026-07-01", "advert_id": 1, "nm_id": 11, "sum": "600"}, {"day": "2026-07-01", "advert_id": 1, "nm_id": 12, "sum": "420"},
          {"day": "2026-07-01", "advert_id": 2, "nm_id": 13, "sum": "1000"},      # кампания с кэшбэком: в вес идёт 30 % (доля «Баланса»)
          {"day": "2026-07-01", "advert_id": 9, "nm_id": 14, "sum": "100"},       # кампания без списаний в день — считается «Балансом» целиком
          {"day": "2026-07-03", "advert_id": 1, "nm_id": 11, "sum": "50"}]         # день без списаний — веса есть, денег нет → ничего

    def test_scripts_import_the_same_functions(self):
        import report_finrez_wb as wbfin
        import report_wb_month as wbm
        self.assertEqual(wbfin.nm_weights(self.NM, alloc.ads_split_of(self.UPD)["advert_day"]), alloc.nm_weights(self.NM, alloc.ads_split_of(self.UPD)["advert_day"]))
        self.assertEqual(wbm.ads_split_of(self.UPD), alloc.ads_split_of(self.UPD))
        self.assertEqual(wbm.row_day({"sale_dt": "2026-09-09T21:30:00+00:00", "rr_date": "2026-09-10"}), "2026-09-10")

    def test_split_weights_and_allocation_sum_to_balance_of_the_day(self):
        split = alloc.ads_split_of(self.UPD)
        self.assertEqual(split["balance"], {"2026-07-01": Decimal("1300"), "2026-07-02": Decimal("500")})
        self.assertEqual(split["advert_day"][("2026-07-01", 2)], (Decimal("300"), Decimal("1000")))
        w = alloc.nm_weights(self.NM, split["advert_day"], "balance")
        self.assertEqual(w["2026-07-01"], {11: Decimal("600"), 12: Decimal("420"), 13: Decimal("300"), 14: Decimal("100")})
        out = alloc.nm_ads_by_day_and_nm(split["balance"], w)
        self.assertEqual(sum(v for (d, _n), v in out.items() if d == "2026-07-01"), Decimal("1300"))
        self.assertEqual(out[("2026-07-01", 11)], Decimal("549.30"))          # 1300 × 600 / 1420
        self.assertNotIn(("2026-07-03", 11), out)                               # нет списаний — нет рекламы
        self.assertEqual(alloc.days_without_weights(split["balance"], w), {"2026-07-02": Decimal("500")})   # списания без статистики — вслух


class SkuOtherRulesTests(unittest.TestCase):
    def test_rows_with_nm_take_other_amount_by_sale_day_msk_and_review_writeoffs_take_nm_from_text(self):
        rows = [{"rr_date": "2026-09-10", "sale_dt": "2026-09-09T21:30:00+00:00", "nm_id": 5, "penalty": "70", "deduction": "0", "additional_payment": "20", "bonus_type_name": ""},
                {"rr_date": "2026-04-03", "sale_dt": "2026-04-03T03:17:08+00:00", "nm_id": 0, "penalty": "0", "deduction": "4860.48", "additional_payment": "0",
                 "bonus_type_name": "Списание за отзыв pYSerzoDhMulsR5Cj77W: акция №1622923, товар 561740756"},
                {"rr_date": "2026-09-24", "sale_dt": None, "nm_id": 0, "penalty": "0", "deduction": "42990", "additional_payment": "0", "bonus_type_name": "Предоставление услуг по подписке «Джем», документ № 1"},
                {"rr_date": "2026-06-13", "sale_dt": None, "nm_id": 7, "penalty": "0", "deduction": "0", "additional_payment": "0", "bonus_type_name": ""},
                {"rr_date": "2026-02-20", "sale_dt": None, "nm_id": 0, "penalty": "0", "deduction": "0", "additional_payment": "7119571.15", "bonus_type_name": ""}]
        self.assertEqual(rules.sku_other_rows(rows), [("2026-09-10", 5, Decimal("50"), "row"), ("2026-04-03", 561740756, Decimal("4860.48"), "review_text")])

    def test_review_nm_parser(self):
        self.assertEqual(rules.review_writeoff_nm("Списание за отзыв X: акция №1, товар 115923383"), 115923383)
        self.assertIsNone(rules.review_writeoff_nm("Витрина Магазина, документ № 3"))
        self.assertIsNone(rules.review_writeoff_nm("Списание за отзыв без товара"))
        self.assertIn("penalty.neq.0", rules.SKU_OTHER_FILTER); self.assertIn("Списание за отзыв*", rules.SKU_OTHER_FILTER)


class ShowcaseTests(unittest.TestCase):
    def test_storage_ads_other_land_on_wb_keys_new_keys_are_created_zero_storage_key_is_not(self):
        orders = [{"order_date": "2026-09-01", "marketplace_code": "wb", "marketplace_sku": "101", "article": "A-101", "orders_qty": 1, "orders_amount_seller": 1000, "cancelled_orders_qty": 0}]
        wb = {"storage": [{"day": "2026-09-01", "nm_id": 101, "warehouse_price": "12.5", "vendor_code": "A-101"},
                          {"day": "2026-09-01", "nm_id": 102, "warehouse_price": "-3.25", "vendor_code": "B-102"},      # сторно: минус, ключ создаётся
                          {"day": "2026-09-02", "nm_id": 103, "warehouse_price": "0", "vendor_code": "C-103"}],          # нетто ноль, ключа нет — не создаём
              "ads": {("2026-09-01", 101): Decimal("549.30"), ("2026-09-01", 104): Decimal("750.70")},
              "other": [("2026-09-01", 101, Decimal("50"), "row"), ("2026-09-03", 105, Decimal("4860.48"), "review_text")],
              "articles": {101: "A-101", 102: "B-102", 103: "C-103", 104: "D-104"}}
        rows = build(wb, orders=orders)
        r = rows[("2026-09-01", "wb", "101")]
        self.assertEqual((r["storage_cost"], r["ad_spend"], r["other_expenses_amount"], r["orders_amount_seller"], r["article"]), (12.5, 549.3, 50.0, 1000.0, "A-101"))
        self.assertEqual((rows[("2026-09-01", "wb", "102")]["storage_cost"], rows[("2026-09-01", "wb", "102")]["article"]), (-3.25, "B-102"))
        self.assertNotIn(("2026-09-02", "wb", "103"), rows)
        self.assertEqual((rows[("2026-09-01", "wb", "104")]["ad_spend"], rows[("2026-09-01", "wb", "104")]["article"]), (750.7, "D-104"))
        self.assertEqual((rows[("2026-09-03", "wb", "105")]["other_expenses_amount"], rows[("2026-09-03", "wb", "105")]["article"]), (4860.48, ""))   # артикула нет нигде — пусто, не выдумываем
        self.assertEqual(r["ad_share_of_orders"], round(549.3 / 1000, 4))

    def test_orders_article_wins_over_vendor_code_and_ozon_rows_are_untouched(self):
        orders = [{"order_date": "2026-09-01", "marketplace_code": "wb", "marketplace_sku": "101", "article": "FROM-ORDERS", "orders_qty": 1, "orders_amount_seller": 10, "cancelled_orders_qty": 0}]
        placement = [{"day": "2026-09-01", "sku": 555, "warehouse": "w", "amount": "-2.00", "offer_id": "O"}]
        wb = {"storage": [{"day": "2026-09-05", "nm_id": 101, "warehouse_price": "1", "vendor_code": "FROM-STORAGE"}], "ads": {}, "other": [], "articles": {101: "FROM-STORAGE"}}
        rows = build(wb, orders=orders, placement=placement)
        self.assertEqual(rows[("2026-09-05", "wb", "101")]["article"], "FROM-ORDERS")
        self.assertEqual((rows[("2026-09-01", "ozon", "555")]["storage_cost"], rows[("2026-09-01", "ozon", "555")]["ad_spend"]), (2.0, 0))

    def test_unreadable_sources_are_marked_and_yield_nothing(self):
        kpi.SOURCE_READ_FAILURES.clear()
        with mock.patch.object(kpi.keyset, "read_keyset", side_effect=RuntimeError("PGRST205 нет вьюхи")), \
             mock.patch.object(kpi, "read_all_by_key", side_effect=RuntimeError("PGRST205")), mock.patch("builtins.print"):
            self.assertEqual(kpi.load_wb_storage(), [])
            self.assertEqual(kpi.load_wb_ads(), {})
            self.assertEqual(kpi.load_wb_sku_other(), [])
        self.assertEqual(kpi.SOURCE_READ_FAILURES, [kpi.WB_STORAGE_VIEW, kpi.WB_ADS_TABLE, kpi.WB_REPORT_TABLE])
        kpi.SOURCE_READ_FAILURES.clear()

    def test_readers_ask_the_views_by_key_with_the_sku_other_filter(self):
        calls = []

        def fake_keyset(sb, table, select, d1, d2, day_col="day", id_col=None, page=1000, filters=None):
            calls.append((table, select, day_col, id_col, filters)); return []
        with mock.patch.object(kpi.keyset, "read_keyset", side_effect=fake_keyset), mock.patch.object(kpi, "read_all_by_key", return_value=[]), mock.patch("builtins.print"):
            kpi.load_wb_costs()
        self.assertEqual([c[0] for c in calls], [kpi.WB_STORAGE_VIEW, kpi.WB_ADS_NM_VIEW, kpi.WB_REPORT_TABLE])
        self.assertEqual(calls[0][2:4], ("day", "nm_id")); self.assertEqual(calls[1][2:4], ("day", "campaign_nm_key"))
        self.assertEqual(calls[2][2:], ("rr_date", "rrd_id", [("or_", rules.SKU_OTHER_FILTER)]))

    def test_ads_loader_allocates_with_the_book_rule_and_names_silent_days(self):
        upd = [{"upd_day": "2026-07-01", "advert_id": 1, "upd_sum": "1000", "payment_type": "Баланс"}, {"upd_day": "2026-07-02", "advert_id": 1, "upd_sum": "5", "payment_type": "Баланс"}]
        nm = [{"day": "2026-07-01", "advert_id": 1, "nm_id": 11, "sum": "30"}, {"day": "2026-07-01", "advert_id": 1, "nm_id": 12, "sum": "10"}]
        printed = []
        with mock.patch.object(kpi, "read_all_by_key", return_value=upd), mock.patch.object(kpi.keyset, "read_keyset", return_value=nm), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            out = kpi.load_wb_ads()
        self.assertEqual(out, {("2026-07-01", 11): Decimal("750.00"), ("2026-07-01", 12): Decimal("250.00")})
        self.assertTrue(any("без статистики" in p and "2026-07-02 5.00" in p for p in printed), printed)

    def test_sql_file_has_both_views_and_the_partial_index(self):
        text = open("sql/20261023_create_wb_cost_views_for_kpi.sql", encoding="utf-8").read()
        for needle in ("create or replace view public.wb_paid_storage_nm_daily", "create or replace view public.wb_ad_spend_nm_campaign_daily",
                       "campaign_nm_key", "idx_wb_sales_report_rows_sku_other", "bonus_type_name like 'Списание за отзыв%'"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
