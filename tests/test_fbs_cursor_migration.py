"""Загрузчик FBS на /v4/posting/fbs/list: курсор, страница 100, паузы, 429, предел страниц.

Та же механика, что у FBO на /v3 (tests/test_fbo_cursor_migration.py); цена в /v4
— объект {amount, currency}, как у FBO, и float() на ней падал бы.
"""
import unittest
from unittest import mock

import loaders.ozon_fbs_orders_loader as fbs


def _resp(payload, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    r.text = ""
    return r


class PagingTests(unittest.TestCase):
    def test_uses_v4_endpoint_and_page_maximum(self):
        captured = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["limit"] = (kw.get("json") or {}).get("limit")
            captured["filter"] = (kw.get("json") or {}).get("filter")
            return _resp({"postings": [], "cursor": "", "has_next": False})

        with mock.patch.object(fbs.http_retry, "post", side_effect=fake_post), \
             mock.patch.object(fbs.time, "sleep"):
            fbs.get_ozon_fbs_postings(days_back=1)
        self.assertIn("/v4/posting/fbs/list", captured["url"])
        self.assertLessEqual(captured["limit"], 100)
        self.assertIn("since", captured["filter"])
        self.assertIn("to", captured["filter"])

    def test_cursor_is_followed_until_has_next_false(self):
        pages = [
            _resp({"postings": [{"posting_number": "a"}], "cursor": "c1", "has_next": True}),
            _resp({"postings": [{"posting_number": "b"}], "cursor": "c2", "has_next": False}),
        ]
        sent = []

        def fake_post(url, **kw):
            sent.append((kw.get("json") or {}).get("cursor"))
            return pages.pop(0)

        with mock.patch.object(fbs.http_retry, "post", side_effect=fake_post), \
             mock.patch.object(fbs.time, "sleep"):
            got = fbs.get_ozon_fbs_postings(days_back=1)
        self.assertEqual([p["posting_number"] for p in got], ["a", "b"])
        self.assertEqual(sent, [None, "c1"])

    def test_empty_cursor_with_has_next_raises(self):
        with mock.patch.object(fbs.http_retry, "post", return_value=_resp({"postings": [], "cursor": "", "has_next": True})), \
             mock.patch.object(fbs.time, "sleep"), \
             self.assertRaises(RuntimeError):
            fbs.get_ozon_fbs_postings(days_back=1)

    def test_page_limit_raises_instead_of_partial(self):
        endless = _resp({"postings": [{"posting_number": "x"}], "cursor": "c", "has_next": True})
        with mock.patch.object(fbs, "MAX_PAGES", 3), \
             mock.patch.object(fbs.http_retry, "post", return_value=endless), \
             mock.patch.object(fbs.time, "sleep"), \
             self.assertRaises(RuntimeError) as ctx:
            fbs.get_ozon_fbs_postings(days_back=1)
        self.assertIn("предел", str(ctx.exception))

    def test_429_is_waited_out_then_succeeds(self):
        seq = [_resp({}, 429), _resp({"postings": [{"posting_number": "a"}], "cursor": "", "has_next": False})]
        with mock.patch.object(fbs.http_retry, "post", side_effect=seq), \
             mock.patch.object(fbs.time, "sleep") as slept:
            got = fbs.get_ozon_fbs_postings(days_back=1)
        self.assertEqual(len(got), 1)
        self.assertIn(fbs.ANTISPAM_PAUSE_SECONDS, [c.args[0] for c in slept.call_args_list])

    def test_429_gives_up_after_three_attempts(self):
        with mock.patch.object(fbs.http_retry, "post", return_value=_resp({}, 429)), \
             mock.patch.object(fbs.time, "sleep"), \
             self.assertRaises(RuntimeError) as ctx:
            fbs.get_ozon_fbs_postings(days_back=1)
        self.assertIn("429", str(ctx.exception))

    def test_http_error_raises_not_partial(self):
        """Старый загрузчик на ошибке молча возвращал накопленное; теперь падает."""
        with mock.patch.object(fbs.http_retry, "post", return_value=_resp({}, 500)), \
             mock.patch.object(fbs.time, "sleep"), \
             self.assertRaises(RuntimeError):
            fbs.get_ozon_fbs_postings(days_back=1)

    def test_explicit_window(self):
        from datetime import datetime, timezone
        captured = {}

        def fake_post(url, **kw):
            captured["filter"] = (kw.get("json") or {}).get("filter")
            return _resp({"postings": [], "cursor": "", "has_next": False})

        with mock.patch.object(fbs.http_retry, "post", side_effect=fake_post), \
             mock.patch.object(fbs.time, "sleep"):
            fbs.get_ozon_fbs_postings(since=datetime(2026, 4, 1, tzinfo=timezone.utc), to=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.assertEqual(captured["filter"], {"since": "2026-04-01T00:00:00.000Z", "to": "2026-05-01T00:00:00.000Z"})


class SaveTests(unittest.TestCase):
    def test_save_writes_both_pairs_with_upsert_on_old_key(self):
        p_ok = {"posting_number": "A", "status": "delivered", "in_process_at": "2026-09-10T08:00:00Z",
                "products": [{"sku": 5, "offer_id": "F5", "name": "n", "quantity": 1, "price": {"amount": "1000", "currency": "RUB"}}]}
        p_cancel = dict(p_ok, posting_number="B", status="cancelled")
        table = mock.Mock()
        table.upsert.return_value.execute.return_value = None
        with mock.patch.object(fbs.supabase, "table", return_value=table):
            fbs.save_ozon_orders([p_ok, p_cancel], observed_at="t")
        rows, kwargs = table.upsert.call_args.args[0], table.upsert.call_args.kwargs
        self.assertEqual(kwargs["on_conflict"], "order_date,marketplace_code,marketplace_sku,order_schema")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["orders_qty"], rows[0]["cancelled_orders_qty"], rows[0]["observed_at"]), (1.0, 1.0, "t"))

    def test_default_window_is_30_days(self):
        self.assertEqual(fbs.DEFAULT_DAYS_BACK, 30)


if __name__ == "__main__":
    unittest.main()
