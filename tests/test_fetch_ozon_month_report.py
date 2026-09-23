"""Книга из Telegram на диск владельца: строка доставки ↔ парсер скачивания, выбор строки, сверка хэша.

Формат строки «книга в Telegram: …» — контракт двух скриптов: доставка печатает, скачивание ищет её в логе Render.
Скачанное с другим sha256 или размером не сохраняется.
"""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


send = load("send_ozon_month_report")
fetch = load("fetch_ozon_month_report")
BOOK = b"PK\x03\x04 fake xlsx bytes"
SHA = hashlib.sha256(BOOK).hexdigest()
DOC = {"file_id": "BQACAgIAAxkDAAIBHmbook", "file_unique_id": "AgADuniq", "file_size": len(BOOK), "file_name": "ozon_2026-09_to_2026-09-23.xlsx"}


def response(status=200, body=None, content=b""):
    r = mock.Mock(status_code=status, content=content, text="")
    r.json.return_value = body or {}
    return r


class DeliveryLineContract(unittest.TestCase):
    def test_line_printed_by_delivery_is_parsed_by_fetch(self):
        line = send.delivery_line(DOC["file_name"], DOC, len(BOOK), SHA)
        self.assertEqual(fetch.parse_delivery_line("2026-09-24T07:32:30Z " + line),
                         {"name": DOC["file_name"], "file_id": DOC["file_id"], "file_unique_id": DOC["file_unique_id"],
                          "size": len(BOOK), "sha256": SHA})

    def test_send_document_answer_gives_file_id(self):
        body = {"ok": True, "result": {"message_id": 286, "document": dict(DOC, thumb={})}}
        self.assertEqual(send.sent_document(body), DOC)
        self.assertEqual(send.sent_document({"ok": True, "result": {}}), {k: None for k in DOC})

    def test_sha256_of_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(BOOK)
        try:
            self.assertEqual(send.sha256_of(fh.name), SHA)
        finally:
            os.unlink(fh.name)

    def test_delivery_is_recorded_next_to_the_book(self):
        with tempfile.TemporaryDirectory() as d:
            book = os.path.join(d, DOC["file_name"])
            json.dump({"buyouts": {"turnover": 1}}, open(book + ".json", "w"))
            send.record_delivery(book, DOC, len(BOOK), SHA)
            data = json.load(open(book + ".json"))
        self.assertEqual(data["buyouts"], {"turnover": 1})
        self.assertEqual((data["delivery"]["file_id"], data["delivery"]["sha256"], data["delivery"]["size"]), (DOC["file_id"], SHA, len(BOOK)))


class PickDelivery(unittest.TestCase):
    def test_latest_line_of_the_right_book_wins(self):
        other = send.delivery_line("ozon_2026-09_to_2026-09-22.xlsx", dict(DOC, file_id="OLD"), 1, "0" * 64)
        first = send.delivery_line(DOC["file_name"], dict(DOC, file_id="FIRST"), 1, "1" * 64)
        second = send.delivery_line(DOC["file_name"], dict(DOC, file_id="SECOND"), 1, "2" * 64)
        lines = [("2026-09-24T07:32:00Z", first), ("2026-09-24T09:00:00Z", second), ("2026-09-24T07:31:00Z", other), ("2026-09-24T07:30:00Z", "шум")]
        self.assertEqual(fetch.pick_delivery(lines, DOC["file_name"])["file_id"], "SECOND")
        self.assertIsNone(fetch.pick_delivery(lines, "ozon_2026-09_to_2026-09-30.xlsx"))

    def test_book_name_follows_the_delivery_rule(self):
        self.assertEqual(fetch.book_name("2026-09-23"), "ozon_2026-09_to_2026-09-23.xlsx")


class Download(unittest.TestCase):
    def run_main(self, argv, log_lines, content):
        log = response(body={"logs": [{"timestamp": ts, "message": m} for ts, m in log_lines], "hasMore": False})
        get_file = response(body={"ok": True, "result": {"file_path": "documents/file_1.xlsx", "file_size": len(content)}})
        data = response(content=content)

        def get(url, **kw):
            if "api.render.com" in url:
                return log
            return get_file if url.endswith("/getFile") else data
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "RENDER_API_KEY": "r"}), \
                mock.patch.object(fetch.requests, "get", side_effect=get), mock.patch("builtins.print"):
            code = fetch.main(argv + ["--out-dir", d])
            files = sorted(os.listdir(d))
            meta = json.load(open(os.path.join(d, DOC["file_name"] + ".download.json"))) if files else None
        return code, files, meta

    def test_matching_hash_saves_the_book_and_the_sidecar(self):
        line = send.delivery_line(DOC["file_name"], DOC, len(BOOK), SHA)
        code, files, meta = self.run_main(["--date-to", "2026-09-23"], [("2026-09-24T07:32:30Z", line)], BOOK)
        self.assertEqual(code, 0)
        self.assertEqual(files, [DOC["file_name"], DOC["file_name"] + ".download.json"])
        self.assertEqual((meta["sha256"], meta["sha256_sent"], meta["size"], meta["source"]), (SHA, SHA, len(BOOK), "лог Render"))

    def test_different_bytes_are_not_saved(self):
        line = send.delivery_line(DOC["file_name"], DOC, len(BOOK), SHA)
        code, files, _meta = self.run_main(["--date-to", "2026-09-23"], [("2026-09-24T07:32:30Z", line)], BOOK + b"x")
        self.assertEqual((code, files), (1, []))

    def test_no_delivery_line_is_a_loud_failure(self):
        code, files, _meta = self.run_main(["--date-to", "2026-09-23"], [], BOOK)
        self.assertEqual((code, files), (1, []))


if __name__ == "__main__":
    unittest.main()
