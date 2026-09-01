import unittest

import loaders.ozon_performance_ads_loader as loader


CPO = 'Инструмент "Оплата за заказ"'
CLICKS = "Кампания за клики"


def row(sku, spend, sale, source, qty=1, date="2026-08-30"):
    return {
        "sale_date": date, "ordered_sku": sku, "offer_id": f"A-{sku}",
        "product_name": "товар", "spend": spend, "sale_amount": sale,
        "quantity": qty, "order_source_raw": source,
    }


class InstrumentRowDetectionTest(unittest.TestCase):
    def test_cpo_instrument_rows_are_recognised(self):
        for value in (CPO, 'инструмент "оплата за заказ"', "ОПЛАТА ЗА ЗАКАЗ"):
            with self.subTest(value=value):
                self.assertTrue(loader.is_selected_cpo_instrument_row({"order_source_raw": value}))

    def test_click_campaign_rows_are_not_cpo(self):
        for value in (CLICKS, "", None, "что-то ещё"):
            with self.subTest(value=value):
                self.assertFalse(loader.is_selected_cpo_instrument_row({"order_source_raw": value}))


class SelectedCpoAttributionRevenueTest(unittest.TestCase):
    def test_revenue_counts_only_cpo_rows_while_spend_counts_all(self):
        rows = [
            row("111", 100.0, 1000.0, CPO),
            row("111", 50.0, 500.0, CLICKS),
        ]
        (attribution,) = loader.build_selected_cpo_ad_attribution_rows(rows)

        self.assertEqual(attribution["ad_spend"], 150.0, "расход — по всем строкам отчёта")
        self.assertEqual(attribution["ad_orders_revenue"], 1000.0, "выручка — только по «Оплате за заказ»")
        self.assertEqual(attribution["ad_orders_qty"], 1.0)

    def test_click_only_sku_keeps_spend_and_zero_revenue(self):
        # Комбо-модель: CPO списан за заказ с кликовой кампании. Выручка этого
        # заказа уже лежит в атрибуции CPC, повторный учёт задвоил бы её.
        (attribution,) = loader.build_selected_cpo_ad_attribution_rows([row("222", 21.0, 214290.0, CLICKS)])
        self.assertEqual(attribution["ad_spend"], 21.0)
        self.assertEqual(attribution["ad_orders_revenue"], 0.0)
        self.assertEqual(attribution["ad_orders_qty"], 0.0)

    def test_rows_without_order_source_do_not_invent_revenue(self):
        source = {"sale_date": "2026-08-30", "ordered_sku": "333", "spend": 10.0, "sale_amount": 999.0}
        (attribution,) = loader.build_selected_cpo_ad_attribution_rows([source])
        self.assertEqual(attribution["ad_spend"], 10.0)
        self.assertEqual(attribution["ad_orders_revenue"], 0.0)

    def test_stale_spend_only_warning_is_gone(self):
        (attribution,) = loader.build_selected_cpo_ad_attribution_rows([row("111", 1.0, 2.0, CPO)])
        self.assertNotIn("warning", attribution)

    def test_totals_are_rounded(self):
        rows = [row("111", 0.1, 0.1, CPO) for _ in range(3)]
        (attribution,) = loader.build_selected_cpo_ad_attribution_rows(rows)
        self.assertEqual(attribution["ad_orders_revenue"], 0.3)
        self.assertEqual(attribution["ad_spend"], 0.3)

    def test_marketplace_expenses_still_counts_every_row(self):
        rows = [row("111", 100.0, 1000.0, CPO), row("111", 50.0, 500.0, CLICKS)]
        (expense,) = loader.build_selected_cpo_marketplace_expenses_rows(rows)
        self.assertEqual(expense["expense_amount"], 150.0)


class SelectedCpo20260830ShapeTest(unittest.TestCase):
    """Форма реального отчёта за 2026-08-30: 22 строки, 17 из них «Оплата за заказ».

    Эталоны Ozon: расход 144 294,20 ₽ по всему отчёту, продажи 1 228 652,00 ₽
    только по инструменту CPO.
    """

    def build(self):
        # Суммы подобраны так, чтобы делиться нацело: равномерное деление на 17
        # даёт артефакт округления, которого в реальном отчёте нет.
        cpo_rows = [row(f"cpo{i}", 7000.00, 72000.00, CPO) for i in range(16)]
        cpo_rows.append(row("cpo16", 10865.20, 76652.00, CPO))
        click_rows = [row(f"clk{i}", 4285.80, 42858.00, CLICKS) for i in range(5)]
        return cpo_rows + click_rows

    def test_reference_totals_reproduce(self):
        rows = self.build()
        attribution = loader.build_selected_cpo_ad_attribution_rows(rows)

        self.assertEqual(len(attribution), 22, "по строке на SKU в этой синтетике")
        self.assertAlmostEqual(sum(r["ad_orders_revenue"] for r in attribution), 1228652.00, places=2)
        self.assertAlmostEqual(sum(r["ad_spend"] for r in attribution), 144294.20, places=2)
        self.assertAlmostEqual(sum(r["ad_orders_qty"] for r in attribution), 17.0, places=6)

    def test_click_revenue_is_excluded_entirely(self):
        rows = self.build()
        attribution = loader.build_selected_cpo_ad_attribution_rows(rows)
        clicks = [r for r in attribution if r["marketplace_sku"].startswith("clk")]
        self.assertEqual(len(clicks), 5)
        self.assertEqual(sum(r["ad_orders_revenue"] for r in clicks), 0.0)
        self.assertAlmostEqual(sum(r["ad_spend"] for r in clicks), 21429.00, places=2)


if __name__ == "__main__":
    unittest.main()
