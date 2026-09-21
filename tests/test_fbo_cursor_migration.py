"""Загрузчик FBO на курсорном методе.

/v2/posting/fbo/list помечен на отключение 31.08.2026. Четыре отличия контракта,
и последнее нашлось только сравнением ЗНАЧЕНИЙ: цена стала объектом
{amount, currency} вместо строки. float() на объекте падает — это лучший исход,
чем молчаливый ноль, но читать надо amount.
"""
import unittest
from unittest import mock

import loaders.ozon_fbo_orders_loader as fbo


class PriceShapeTests(unittest.TestCase):
    def test_object_price_is_read_from_amount(self):
        self.assertEqual(
            fbo.posting_price({"sku": 1, "price": {"amount": "14560", "currency": "RUB"}}),
            14560.0,
        )

    def test_string_price_still_works(self):
        """Старая форма не должна ломаться, пока оба метода живы."""
        self.assertEqual(fbo.posting_price({"sku": 1, "price": "14560.00"}), 14560.0)

    def test_kopecks_survive(self):
        self.assertEqual(
            fbo.posting_price({"sku": 1, "price": {"amount": "1234.56", "currency": "RUB"}}),
            1234.56,
        )

    def test_foreign_currency_raises(self):
        """Чужая валюта — это рубли там, где их нет. Молчать нельзя."""
        with self.assertRaises(RuntimeError) as ctx:
            fbo.posting_price({"sku": 7, "price": {"amount": "100", "currency": "USD"}})
        self.assertIn("USD", str(ctx.exception))

    def test_object_without_amount_raises(self):
        """Объект без amount — сломанный контракт, а не бесплатный товар."""
        for price in ({"currency": "RUB"}, {"amount": None, "currency": "RUB"},
                      {"amount": "", "currency": "RUB"}):
            with self.subTest(price=price), self.assertRaises(RuntimeError) as ctx:
                fbo.posting_price({"sku": 9, "price": price})
            self.assertIn("amount", str(ctx.exception))

    def test_missing_price_is_zero(self):
        self.assertEqual(fbo.posting_price({"sku": 1}), 0.0)


class PagingTests(unittest.TestCase):
    def _resp(self, payload, status=200):
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = payload
        r.text = ""
        return r

    def test_cursor_is_followed_until_has_next_false(self):
        pages = [
            self._resp({"postings": [{"posting_number": "a"}], "cursor": "c1", "has_next": True}),
            self._resp({"postings": [{"posting_number": "b"}], "cursor": "c2", "has_next": False}),
        ]
        with mock.patch.object(fbo.http_retry, "post", side_effect=pages), \
             mock.patch.object(fbo.time, "sleep"):
            got = fbo.get_fbo_postings(days_back=1)
        self.assertEqual([p["posting_number"] for p in got], ["a", "b"])

    def test_empty_cursor_with_has_next_raises(self):
        pages = [self._resp({"postings": [], "cursor": "", "has_next": True})]
        with mock.patch.object(fbo.http_retry, "post", side_effect=pages), \
             mock.patch.object(fbo.time, "sleep"), \
             self.assertRaises(RuntimeError):
            fbo.get_fbo_postings(days_back=1)

    def test_page_limit_raises_instead_of_partial(self):
        """Частичный результат вреднее ошибки: он выглядит как полный."""
        endless = self._resp({"postings": [{"posting_number": "x"}], "cursor": "c", "has_next": True})
        with mock.patch.object(fbo, "MAX_PAGES", 3), \
             mock.patch.object(fbo.http_retry, "post", return_value=endless), \
             mock.patch.object(fbo.time, "sleep"), \
             self.assertRaises(RuntimeError) as ctx:
            fbo.get_fbo_postings(days_back=1)
        self.assertIn("предел", str(ctx.exception))

    def test_429_is_waited_out_then_succeeds(self):
        seq = [self._resp({}, 429),
               self._resp({"postings": [{"posting_number": "a"}], "cursor": "", "has_next": False})]
        with mock.patch.object(fbo.http_retry, "post", side_effect=seq), \
             mock.patch.object(fbo.time, "sleep") as slept:
            got = fbo.get_fbo_postings(days_back=1)
        self.assertEqual(len(got), 1)
        self.assertIn(fbo.ANTISPAM_PAUSE_SECONDS, [c.args[0] for c in slept.call_args_list])

    def test_429_gives_up_after_three_attempts(self):
        with mock.patch.object(fbo.http_retry, "post", return_value=self._resp({}, 429)), \
             mock.patch.object(fbo.time, "sleep"), \
             self.assertRaises(RuntimeError) as ctx:
            fbo.get_fbo_postings(days_back=1)
        self.assertIn("429", str(ctx.exception))

    def test_page_size_is_the_method_maximum(self):
        """Спека: maximum 100. Больше — HTTP 400."""
        captured = {}

        def fake_post(url, **kw):
            captured["limit"] = (kw.get("json") or {}).get("limit")
            return self._resp({"postings": [], "cursor": "", "has_next": False})

        with mock.patch.object(fbo.http_retry, "post", side_effect=fake_post), \
             mock.patch.object(fbo.time, "sleep"):
            fbo.get_fbo_postings(days_back=1)
        self.assertLessEqual(captured["limit"], 100)

    def test_uses_v3_endpoint(self):
        captured = {}

        def fake_post(url, **kw):
            captured["url"] = url
            return self._resp({"postings": [], "cursor": "", "has_next": False})

        with mock.patch.object(fbo.http_retry, "post", side_effect=fake_post), \
             mock.patch.object(fbo.time, "sleep"):
            fbo.get_fbo_postings(days_back=1)
        self.assertIn("/v3/posting/fbo/list", captured["url"])


if __name__ == "__main__":
    unittest.main()
