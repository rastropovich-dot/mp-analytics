"""Одно правило заказов на обе схемы: подтверждённые и отменённые рядом.

Корень прежней поломки — два правила: FBO выбрасывал cancelled и оставлял
пустые ключи, FBS складывал отменённые вместе с доставленными.
"""
import unittest

from loaders import ozon_orders_rows as rules


def posting(number, status, schema="fbo", when="2026-09-10T08:00:00Z", products=None, price="1000", qty=1, sku=11):
    p = {"posting_number": number, "status": status,
         "products": products if products is not None else [
             {"sku": sku, "offer_id": f"F{sku}", "name": "товар", "quantity": qty,
              "price": {"amount": price, "currency": "RUB"} if schema == "fbo" else price}]}
    if schema == "fbo":
        p["created_at"] = when
        p["in_process_at"] = when
    else:
        p["in_process_at"] = when
        p["shipment_date"] = "2026-09-11T19:00:00Z"
    return p


class OneRuleTests(unittest.TestCase):
    def test_confirmed_and_cancelled_land_in_their_own_pair(self):
        rows, c = rules.build_order_rows([posting("A", "delivered"), posting("B", "cancelled", price="500")], "fbo", observed_at="t")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["orders_qty"], r["orders_amount_seller"]), (1.0, 1000.0))
        self.assertEqual((r["cancelled_orders_qty"], r["cancelled_orders_amount_seller"]), (1.0, 500.0))
        self.assertEqual(r["observed_at"], "t")
        self.assertEqual((c["confirmed_postings"], c["cancelled_postings"]), (1, 1))

    def test_same_rule_for_fbs(self):
        """FBS раньше не смотрел на статус — теперь отменённые уходят в свою пару."""
        rows, c = rules.build_order_rows([posting("A", "delivered", "fbs"), posting("B", "cancelled", "fbs")], "fbs")
        self.assertEqual((rows[0]["orders_qty"], rows[0]["cancelled_orders_qty"]), (1.0, 1.0))
        self.assertEqual(rows[0]["order_schema"], "fbs")

    def test_fully_cancelled_key_still_gets_a_row(self):
        """Ключ, у которого все отправления отменены, не исчезает — иначе снова устаревшие ключи."""
        rows, _ = rules.build_order_rows([posting("B", "cancelled")], "fbo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders_qty"], 0.0)
        self.assertEqual(rows[0]["cancelled_orders_qty"], 1.0)

    def test_maturation_moves_amount_between_pairs_on_the_same_key(self):
        """Вчера заказ был подтверждённым, сегодня отменён: тот же ключ, суммы переложены."""
        day1, _ = rules.build_order_rows([posting("A", "delivering")], "fbo")
        day2, _ = rules.build_order_rows([posting("A", "cancelled")], "fbo")
        key = lambda r: (r["order_date"], r["marketplace_sku"], r["order_schema"])  # noqa: E731
        self.assertEqual(key(day1[0]), key(day2[0]))
        self.assertEqual((day1[0]["orders_qty"], day1[0]["cancelled_orders_qty"]), (1.0, 0.0))
        self.assertEqual((day2[0]["orders_qty"], day2[0]["cancelled_orders_qty"]), (0.0, 1.0))

    def test_cohort_date_is_order_date_not_cancellation_date(self):
        rows, _ = rules.build_order_rows([posting("A", "cancelled", when="2026-09-01T10:00:00Z")], "fbo")
        self.assertEqual(rows[0]["order_date"], "2026-09-01")

    def test_local_date_from_utc(self):
        """23:30 UTC — уже следующий день по Москве."""
        rows, _ = rules.build_order_rows([posting("A", "delivered", when="2026-09-10T23:30:00Z")], "fbo")
        self.assertEqual(rows[0]["order_date"], "2026-09-11")

    def test_fbs_date_from_in_process_at(self):
        p = posting("A", "delivered", "fbs", when="2026-09-05T10:00:00Z")
        rows, _ = rules.build_order_rows([p], "fbs")
        self.assertEqual(rows[0]["order_date"], "2026-09-05")

    def test_split_parent_is_skipped_and_counted(self):
        rows, c = rules.build_order_rows([posting("P", "cancelled_from_split_pending")], "fbs")
        self.assertEqual(rows, [])
        self.assertEqual(c["split_parent_skipped"], 1)

    def test_unknown_status_is_confirmed_and_named_aloud(self):
        rows, c = rules.build_order_rows([posting("A", "some_new_status")], "fbo")
        self.assertEqual(rows[0]["orders_qty"], 1.0)
        self.assertEqual(c["unknown_statuses"], {"some_new_status": 1})

    def test_every_spec_status_is_classified(self):
        for status in rules.CONFIRMED_STATUSES | rules.CANCELLED_STATUSES | rules.SPLIT_PARENT_STATUSES:
            _, c = rules.build_order_rows([posting("A", status)], "fbs")
            self.assertEqual(c["unknown_statuses"], {}, status)

    def test_price_forms_string_and_object(self):
        rows, _ = rules.build_order_rows([posting("A", "delivered", "fbs", price="1234.56")], "fbs")
        self.assertEqual(rows[0]["orders_amount_seller"], 1234.56)
        rows, _ = rules.build_order_rows([posting("A", "delivered", "fbo", price="1234.56", qty=2)], "fbo")
        self.assertEqual(rows[0]["orders_amount_seller"], 2469.12)

    def test_foreign_currency_raises(self):
        p = posting("A", "delivered")
        p["products"][0]["price"] = {"amount": "100", "currency": "USD"}
        with self.assertRaises(RuntimeError):
            rules.build_order_rows([p], "fbo")

    def test_object_without_amount_raises(self):
        p = posting("A", "delivered")
        p["products"][0]["price"] = {"currency": "RUB"}
        with self.assertRaises(RuntimeError):
            rules.build_order_rows([p], "fbo")

    def test_zero_qty_and_missing_sku_are_counted_not_written(self):
        p = posting("A", "delivered", products=[{"sku": 1, "quantity": 0, "price": {"amount": "10", "currency": "RUB"}},
                                                 {"offer_id": "X", "quantity": 1, "price": {"amount": "10", "currency": "RUB"}}])
        rows, c = rules.build_order_rows([p], "fbo")
        self.assertEqual(rows, [])
        self.assertEqual((c["zero_qty"], c["no_sku"]), (1, 1))

    def test_no_date_is_counted(self):
        p = posting("A", "delivered"); p["created_at"] = None; p["in_process_at"] = None
        rows, c = rules.build_order_rows([p], "fbo")
        self.assertEqual((rows, c["no_date"]), ([], 1))

    def test_two_postings_same_key_are_summed(self):
        rows, _ = rules.build_order_rows([posting("A", "delivered"), posting("B", "delivered", qty=2)], "fbo")
        self.assertEqual(rows[0]["orders_qty"], 3.0)
        self.assertEqual(rows[0]["orders_amount_seller"], 3000.0)

    def test_sums_helper(self):
        rows, _ = rules.build_order_rows([posting("A", "delivered"), posting("B", "cancelled", price="500")], "fbo")
        self.assertEqual([str(x) for x in rules.sums(rows)], ["1.0", "1000.0", "1.0", "500.0"])

    def test_bad_schema_raises(self):
        with self.assertRaises(ValueError):
            rules.build_order_rows([], "wb")


if __name__ == "__main__":
    unittest.main()


class ExtraFieldsTests(unittest.TestCase):
    """Ozon добавляет поля в ответы (2026-09: customer_price в /v3/posting/fbs/list и /v3/posting/fbs/get, чат 09-18…09-23).
    Загрузчики берут своё по имени и на лишнее не смотрят — строки те же, что без лишних полей."""

    def test_unknown_fields_on_posting_and_products_change_nothing(self):
        plain = [posting("A", "delivered", schema="fbs"), posting("B", "cancelled", schema="fbs", price="500")]
        noisy = [posting("A", "delivered", schema="fbs"), posting("B", "cancelled", schema="fbs", price="500")]
        for p in noisy:
            p["some_new_flag"] = True
            p["financial_data"] = {"products": [{"product_id": 11, "customer_price": "700", "payout": "0", "actions": ["OA"]}]}
            for pr in p["products"]:
                pr["customer_price"] = "700"
                pr["jewelry_codes"] = ["x"]
        rows_plain, c_plain = rules.build_order_rows(plain, "fbs", observed_at="t")
        rows_noisy, c_noisy = rules.build_order_rows(noisy, "fbs", observed_at="t")
        self.assertEqual(rows_noisy, rows_plain)
        self.assertEqual(c_noisy, c_plain)
        fbo_plain = rules.build_order_rows([posting("C", "delivered")], "fbo", observed_at="t")[0]
        noisy_fbo = posting("C", "delivered")
        noisy_fbo["products"][0]["unknown_price_field"] = {"amount": "1", "currency": "RUB"}
        noisy_fbo["storage_type"] = "new"
        self.assertEqual(rules.build_order_rows([noisy_fbo], "fbo", observed_at="t")[0], fbo_plain)
