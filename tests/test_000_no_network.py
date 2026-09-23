"""Тесты не открывают сеть и не видят боевых ключей. Модуль называется test_000_…, чтобы discover импортировал его
ПЕРВЫМ: всё ниже выполняется до импорта любого модуля проекта.

Зачем. 2026-09-23 полный набор с заблокированными сокетами дал 118 попыток соединения с боевой Supabase из 34
тестов (только чтения), и в тот же день тест, вызывавший run_daily_pipeline.main(), записал строку в боевую
pipeline_runtime_state — одна правка превратила чтение в запись. Правило (тридцать четвёртая задача, §2):

  * ключи — фальшивые: модули проекта зовут load_dotenv(), а он НЕ перезаписывает уже заданные переменные,
    поэтому значения ниже, выставленные до импорта, закрывают боевой .env целиком;
  * любое сетевое соединение (TCP/UDP, IPv4/IPv6, разрешение имён) — ошибка NetworkBlockedInTests с тем,
    куда пытались сойти; Unix-сокеты не трогаются;
  * тест, которому нужна живая база, помечается @live_only и по умолчанию пропускается; MP_TESTS_LIVE=1 снимает
    и пропуск, и блокировку (и тогда .env не закрывается — живому тесту нужны настоящие ключи).

Запускать набор как всегда: venv/bin/python3 -m unittest discover -s tests. Модуль отдельного теста
(`python -m unittest tests.test_x`) этот файл не импортирует — там защита не действует; полный набор — действует,
и test_guard_is_active это проверяет.
"""
import os
import socket
import unittest

LIVE = os.getenv("MP_TESTS_LIVE") == "1"

# Все секреты .env и запасные имена, которые читает код (grep os.getenv по проекту, 2026-09-23).
FAKE_ENV = {
    "SUPABASE_URL": "http://supabase.tests.invalid",
    "SUPABASE_SERVICE_KEY": "fake.test.key",
    "SUPABASE_SERVICE_ROLE_KEY": "fake.test.key",
    "SUPABASE_KEY": "fake.test.key",
    "OZON_CLIENT_ID": "0",
    "OZON_API_KEY": "fake-ozon-api-key",
    "OZON_PERFORMANCE_CLIENT_ID": "fake-performance-client",
    "OZON_PERFORMANCE_CLIENT_SECRET": "fake-performance-secret",
    "WB_API_KEY": "fake-wb-api-key",
    "TELEGRAM_BOT_TOKEN": "0:fake-telegram-token",
    "TELEGRAM_CHAT_ID": "0",
    "RENDER_API_KEY": "fake-render-key",
    "OPENAI_API_KEY": "fake-openai-key",
}


# Каждая попытка — (что, куда, кадры проекта). Код проекта местами ловит отказ сам, и тест проходит — поэтому
# test_zzz_no_network_attempts.py (последний модуль набора) требует, чтобы список остался пустым.
ATTEMPTS = []


class NetworkBlockedInTests(ConnectionRefusedError):
    """Тест попытался открыть сеть. Подменить вызов заглушкой или пометить тест @live_only."""


def _refuse(what, target):
    import traceback
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    frames = [f"{os.path.relpath(f.filename, root)}:{f.lineno} {f.name}" for f in traceback.extract_stack()[:-2]
              if f.filename.startswith(root) and "/venv/" not in f.filename and not f.filename.endswith("test_000_no_network.py")]
    ATTEMPTS.append((what, str(target), frames[-6:]))
    raise NetworkBlockedInTests(f"сеть в тестах запрещена: {what} {target}")


def _blocked(what):
    def refuse(*args, **kwargs):
        _refuse(what, args[0] if args else kwargs)
    return refuse


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _refuse("connect", address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _refuse("connect_ex", address)
    return _real_connect_ex(self, address)


def install():
    """Фальшивые ключи и блокировка сети. Повторный вызов безопасен."""
    for name, value in FAKE_ENV.items():
        os.environ[name] = value
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.getaddrinfo = _blocked("getaddrinfo")
    socket.create_connection = _blocked("create_connection")
    socket.gethostbyname = _blocked("gethostbyname")


class _EmptyQuery:
    """Любая цепочка PostgREST (select/eq/order/range/upsert/delete…) → execute() с пустым ответом."""
    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def execute(self):
        return type("EmptyResponse", (), {"data": [], "count": 0})()


class EmptySupabase:
    """Клиент базы без сети: всё читается пустым, запись уходит в никуда. Для модулей, где код проекта сам
    ходит в базу мимо аргументов (глобальный supabase загрузчика) и тест это не проверяет: пусто — ровно то,
    что код и так получал при отказе соединения (его ветки except возвращают пустое)."""
    def table(self, name):
        return _EmptyQuery()

    def rpc(self, *args, **kwargs):
        return _EmptyQuery()


def offline_module(module, attribute="supabase"):
    """(setUpModule, tearDownModule) — подменить клиент базы модуля пустым на время файла тестов."""
    from unittest import mock
    patcher = mock.patch.object(module, attribute, EmptySupabase())
    return patcher.start, patcher.stop


def live_only(test):
    """Тест, которому нужна живая база или API: по умолчанию пропускается, MP_TESTS_LIVE=1 — запускается."""
    return unittest.skipUnless(LIVE, "живой тест: MP_TESTS_LIVE=1")(test)


if not LIVE:
    install()


class GuardTests(unittest.TestCase):
    @unittest.skipIf(LIVE, "MP_TESTS_LIVE=1: блокировка снята")
    def test_guard_is_active(self):
        with self.assertRaises(NetworkBlockedInTests):
            socket.create_connection(("192.0.2.1", 443), timeout=1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(NetworkBlockedInTests):
                s.connect(("192.0.2.1", 443))
        finally:
            s.close()
        with self.assertRaises(NetworkBlockedInTests):
            socket.getaddrinfo("example.com", 443)
        del ATTEMPTS[-3:]                      # три попытки выше — нарочные

    @unittest.skipIf(LIVE, "MP_TESTS_LIVE=1: ключи настоящие")
    def test_keys_are_fake(self):
        for name, value in FAKE_ENV.items():
            self.assertEqual(os.environ.get(name), value, name)

    @unittest.skipIf(LIVE, "MP_TESTS_LIVE=1: ключи настоящие")
    def test_project_modules_saw_fake_keys(self):
        import run_daily_pipeline as pipeline
        self.assertEqual(pipeline.TELEGRAM_BOT_TOKEN, FAKE_ENV["TELEGRAM_BOT_TOKEN"])
