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
import re
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


MIGRATIONS_TABLE = "schema_migrations"
SQL_FILE_RE = re.compile(r"^\d{8}_.*\.sql$")          # базовый 00000000_… и датированные; сортировка по имени = порядок применения


def sql_files(sql_dir=None):
    """Имена файлов sql/ в порядке применения (базовый первым)."""
    sql_dir = sql_dir or os.path.join(ROOT, "sql")
    return sorted(fn for fn in os.listdir(sql_dir) if SQL_FILE_RE.match(fn))


def file_sha256(path):
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def schema_status(client, prof=None, sql_dir=None, environ=None):
    """Версия схемы базы кабинета против файлов sql/ репозитория (5.2б): одно чтение schema_migrations через PostgREST.
    Возвращает (state, message, missing):
      skipped   — тестовый хост (.invalid), к базе не ходили;
      untracked — таблицы schema_migrations нет: версии не отслеживаются, НЕ останавливать (поправка советника 1: пока KARATOV не засеян);
      ok        — все файлы sql/ применены;
      behind    — не применены файлы (список в missing) — пайплайн останавливается, алерт шлёт только эту строку;
      error     — чтение не удалось по другой причине (сеть, права): сказать вслух, не останавливать.
    Никогда не бросает."""
    prof = prof or profile()
    environ = os.environ if environ is None else environ
    name = prof.DISPLAY_NAME
    if supabase_host(environ.get("SUPABASE_URL")).endswith(TEST_HOST_SUFFIX):
        return "skipped", f"схема: проверка версий пропущена (тестовый хост)", []
    try:
        rows = client.table(MIGRATIONS_TABLE).select("filename").order("filename").limit(1000).execute().data or []
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "PGRST205" in text or "42P01" in text or MIGRATIONS_TABLE in text:
            return "untracked", f"схема: версии не отслеживаются (таблицы {MIGRATIONS_TABLE} в базе {name} нет)", []
        return "error", f"схема: проверка версий не удалась ({type(exc).__name__}: {text[:160]}) — продолжаю", []
    applied = {r["filename"] for r in rows}
    files = sql_files(sql_dir)
    missing = [f for f in files if f not in applied]
    if missing:
        shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        return "behind", f"база кабинета {name} отстаёт от кода: не применены {len(missing)} файлов sql/: {shown} — scripts/db_migrate.py --cabinet {prof.CODE} --apply", missing
    extra = sorted(applied - set(files))
    note = f"; в базе {len(extra)} записей без файла в репозитории" if extra else ""
    return "ok", f"схема: применены все {len(files)} файлов sql/{note}", []


def assert_schema(client, prof=None, sql_dir=None, environ=None):
    """Для пайплайна: строка состояния схемы; отстаёт — SystemExit с той же строкой (код 1). untracked / error — только строка."""
    state, message, _missing = schema_status(client, prof, sql_dir, environ)
    if state == "behind":
        raise SystemExit(message)
    return message


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
