"""Выкупы из accrual/by-day: знаки продаж и возвратов.

Возврат — то же начисление с обратными знаками во всех полях. Комиссия у
продажи отрицательна (удержана), у возврата положительна (возвращена нам).
Модуль здесь превратил бы возврат комиссии в расход.
"""
import unittest

from loaders import ozon_finance_accrual as accrual


def make(sku, sale_amount, sale_commission, delivery=0.0, day="2026-09-07"):
    return {
        "accrual_id": 1,
        "date": day,
        "posting": {"products": [{
            "sku": sku,
            "commission": {
                "sale_amount": {"amount": str(sale_amount), "currency": "RUB"},
                "sale_commission": {"amount": str(sale_commission), "currency": "RUB"},
            },
            "delivery": {"services": [
                {"type_id": 32, "accrued": {"amount": str(delivery), "currency": "RUB"}}
            ]} if delivery else None,
        }]},
    }


class BuyoutRowsTests(unittest.TestCase):
    def test_sale_is_positive_and_commission_stored_positive(self):
        rows, counters = accrual.build_buyout_rows([make(1, 2751, -1237.95)])
        row = rows[0]
        self.assertEqual(row["buyouts_amount_seller"], 2751.0)
        self.assertEqual(row["commission_amount"], 1237.95)
        self.assertEqual(row["buyouts_qty"], 1)
        self.assertEqual(counters.get("sale"), 1)

    def test_return_subtracts_and_returns_commission(self):
        rows, counters = accrual.build_buyout_rows([make(1, -2272, 1067.84)])
        row = rows[0]
        self.assertEqual(row["buyouts_amount_seller"], -2272.0)
        self.assertEqual(row["commission_amount"], -1067.84,
                         "возврат комиссии обязан уменьшать комиссию, а не прибавляться")
        self.assertEqual(row["buyouts_qty"], -1)
        self.assertEqual(counters.get("return"), 1)

    def test_sale_and_return_of_one_sku_net_out(self):
        rows, _ = accrual.build_buyout_rows([make(1, 2751, -1237.95), make(1, -2751, 1237.95)])
        self.assertEqual(rows, [], "продажа и её возврат должны схлопнуться в ноль")

    def test_revenue_includes_delivery_services(self):
        rows, _ = accrual.build_buyout_rows([make(1, 1000, -200, delivery=-50)])
        self.assertEqual(rows[0]["revenue_after_commission_vat"], 750.0)

    def test_quantity_counts_positions_not_pieces(self):
        """Поля штук в новой модели нет — считаем позиции, и это известно."""
        rows, _ = accrual.build_buyout_rows([make(1, 100, -10)])
        self.assertEqual(rows[0]["buyouts_qty"], 1)


if __name__ == "__main__":
    unittest.main()
