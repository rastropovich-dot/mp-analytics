"""Профиль кабинета и защита от перепутывания баз (первая задача RBH, §2; дополнение 09-28).

Один код — несколько кабинетов. Кабинет выбирает переменная окружения MP_CABINET: «karatov» (по умолчанию, когда не задана, —
кабинет по умолчанию ничего не замечает), «rbh1», «rbh2» (РБХ-1: Ozon Попова + WB ИП Рафикова; РБХ-2: Ozon Малимон + WB ИП Плахов; у каждого своя база
Supabase и свой сервис Render, одна группа Telegram на двоих — кабинет пишется в заголовке). Профиль — модуль cabinets/<код>.py, в нём только
несекретное: имена, правила артикулов, константы листов владельца, каталоги данных и логов, ожидаемый хост Supabase. Секреты
живут в .env / .env.<код> / переменных сервиса Render и в профиль не попадают.

assert_env() зовётся в каждой точке входа сразу после load_dotenv(), ДО чтения ключей и создания клиента Supabase:
  1. читает MP_CABINET и берёт профиль;
  2. подгружает общий <корень>/.env (без перезаписи, как load_dotenv()), затем <корень>/.env.<код> ПОВЕРХ окружения (значение файла кабинета главнее общего .env; пустая
     строка в файле кабинета ничего не затирает; у karatov такого файла нет — шаг пропускается; на Render файлов нет —
     действуют переменные сервиса);
  3. сверяет хост SUPABASE_URL с SUPABASE_HOST профиля. Не совпали, URL не задан, у профиля хоста ещё нет — SystemExit с
     внятной строкой (код 1). Хост в зоне .invalid (RFC 2606, никогда не резолвится) — заглушка tests/test_000_no_network.py:
     guard её пропускает, чтобы модули, создающие клиент при импорте, импортировались в наборе тестов без сети и без ключей.
Возвращает профиль. banner(профиль) — строка «кабинет: <имя>» для первой строки лога пайплайна и первой строки алерта.

Загрузчики зовутся пайплайном как `python3 loaders/<файл>.py` (корня проекта нет в sys.path), поэтому там `import cabinet`
стоит с запасным путём — см. блок после load_dotenv() в любом из них.

Шелл-скрипты (finrez_nightly.sh) берут профиль так: `eval "$(venv/bin/python3 cabinet.py --shell)"` — печатает export-строки
MP_CABINET_NAME, MP_DATA_DIR, MP_LOGS_DIR, MP_REPORT_PREFIX, MP_BOOK_MONTH_FROM; guard там же (окружение не сошлось — код 1).
"""
import importlib
import os
import sys
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_VAR = "MP_CABINET"
DEFAULT_CODE = "karatov"
KNOWN_CODES = ("karatov", "rbh1", "rbh2")
TEST_HOST_SUFFIX = ".invalid"


def cabinet_code(environ=None):
    """Код кабинета из окружения: пусто → karatov; регистр не важен; неизвестный код — SystemExit."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR) or ""
    code = raw.strip().lower() or DEFAULT_CODE
    if code not in KNOWN_CODES:
        raise SystemExit(f"{ENV_VAR}={raw!r}: неизвестный кабинет; известны: {', '.join(KNOWN_CODES)}")
    return code


def profile(code=None):
    """Модуль cabinets/<код>.py."""
    code = code or cabinet_code()
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)   # пакет cabinets/ лежит рядом с этим файлом
    return importlib.import_module(f"cabinets.{code}")


def overlay_path(code, root=ROOT):
    return os.path.join(root, f".env.{code}")


def load_base(environ=None, root=ROOT):
    """Общий <корень>/.env без перезаписи — то же, что load_dotenv() в модулях; guard не зависит от того, успел ли модуль
    его вызвать (часть скриптов грузит .env только через импорт загрузчика). Возвращает число установленных переменных;
    None — файла нет."""
    environ = os.environ if environ is None else environ
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return None
    from dotenv import dotenv_values
    count = 0
    for key, value in dotenv_values(path).items():
        if value is None or key in environ:
            continue
        environ[key] = value
        count += 1
    return count


def load_overlay(code, environ=None, root=ROOT):
    """Догрузить .env.<код> поверх окружения. Возвращает число установленных переменных; None — файла нет."""
    environ = os.environ if environ is None else environ
    path = overlay_path(code, root)
    if not os.path.isfile(path):
        return None
    from dotenv import dotenv_values
    count = 0
    for key, value in dotenv_values(path).items():
        if value is None or value == "":
            continue   # пустая строка кабинета не затирает общий .env
        environ[key] = value
        count += 1
    return count


def supabase_host(url):
    """Хост из SUPABASE_URL: 'https://abc.supabase.co/' → 'abc.supabase.co'; строка без схемы тоже принимается."""
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    return (urlsplit(text).hostname or "").lower()


def check_env(environ=None, root=ROOT):
    """(ok, сообщение, профиль): overlay загружен, хост сверен; в отличие от assert_env не выходит."""
    environ = os.environ if environ is None else environ
    code = cabinet_code(environ)
    prof = profile(code)
    load_base(environ, root)
    load_overlay(code, environ, root)
    name = prof.DISPLAY_NAME
    host = supabase_host(environ.get("SUPABASE_URL"))
    if host.endswith(TEST_HOST_SUFFIX):
        return True, f"кабинет {name}: тестовый хост {host}", prof
    if not host:
        return (False, f"кабинет {name}: SUPABASE_URL не задан — заполните {overlay_path(code, root)} (или общий .env), "
                       f"на Render — переменные сервиса", prof)
    expected = (prof.SUPABASE_HOST or "").lower()
    if not expected:
        return (False, f"кабинет {name}: в cabinets/{code}.py не задан SUPABASE_HOST — впишите хост проекта Supabase этого "
                       f"кабинета, до этого запуск невозможен (окружение указывает на {host})", prof)
    if host != expected:
        return (False, f"кабинет {name}: SUPABASE_URL указывает на {host}, а кабинет ждёт {expected} — не тот .env или не тот "
                       f"{ENV_VAR}; клиент Supabase не создан", prof)
    return True, f"кабинет {name}: база {host}", prof


def assert_env(environ=None, root=ROOT):
    """Профиль кабинета, если окружение про него; иначе SystemExit с причиной (код 1) — до создания любого клиента."""
    ok, message, prof = check_env(environ, root)
    if not ok:
        raise SystemExit(message)
    return prof


def banner(prof=None):
    return f"кабинет: {(prof or profile()).DISPLAY_NAME}"


def data_dir(prof=None, root=ROOT):
    return os.path.join(root, (prof or profile()).DATA_DIR)


def logs_dir(prof=None, root=ROOT):
    return os.path.join(root, (prof or profile()).LOGS_DIR)


def logs_path(*parts, prof=None, root=ROOT):
    """Путь внутри каталога логов кабинета: logs_path('x') → <root>/<LOGS_DIR>/x."""
    return os.path.join(logs_dir(prof, root), *parts)


def data_path(*parts, prof=None, root=ROOT):
    """Путь внутри каталога данных кабинета: data_path('reports', 'x.xlsx') → <root>/<DATA_DIR>/reports/x.xlsx."""
    return os.path.join(data_dir(prof, root), *parts)


def shell_exports(prof, root=ROOT):
    """Строки `export …` для шелл-скриптов: имя кабинета, каталоги данных и логов (абсолютные), префикс имён книг, первый
    месяц книги «Фин рез» (пусто — не задан)."""
    import shlex
    values = (("MP_CABINET_NAME", prof.DISPLAY_NAME), ("MP_DATA_DIR", data_dir(prof, root)), ("MP_LOGS_DIR", logs_dir(prof, root)),
              ("MP_REPORT_PREFIX", prof.REPORT_PREFIX or ""), ("MP_BOOK_MONTH_FROM", prof.BOOK_MONTH_FROM or ""))
    return "\n".join(f"export {k}={shlex.quote(str(v))}" for k, v in values)


if __name__ == "__main__":
    if sys.argv[1:] == ["--shell"]:
        ok, message, prof = check_env()
        if not ok:
            print(message, file=sys.stderr)
            sys.exit(1)
        print(shell_exports(prof))
    else:
        ok, message, _prof = check_env()
        print(message)
        sys.exit(0 if ok else 1)
