"""Performance: текст отчёта из ответа — gzip (многодневная выгрузка, gz=true) разжимается по сигнатуре, чистый CSV и BOM читаются как есть."""
import gzip
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "loaders"))
import ozon_performance_ads_loader as loader  # noqa: E402


class Resp:
    def __init__(self, content):
        self.content = content


class ReportTextTests(unittest.TestCase):
    def test_gzip_body_is_decompressed(self):
        csv = "Дата;sku;Расход, ₽\n2026-08-23;1;10,5\n"
        self.assertEqual(loader.report_text(Resp(gzip.compress(csv.encode("utf-8")))), csv)

    def test_plain_csv_with_bom_is_returned_without_bom(self):
        self.assertEqual(loader.report_text(Resp("﻿a;b\n1;2\n".encode("utf-8"))), "a;b\n1;2\n")

    def test_empty_body_gives_empty_text(self):
        self.assertEqual(loader.report_text(Resp(b"")), "")


if __name__ == "__main__":
    unittest.main()
