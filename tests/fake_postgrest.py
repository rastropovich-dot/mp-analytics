"""Подставной PostgREST для офлайн-тестов (п. 44а / 46): хранит строки по первичному ключу и применяет upsert так же, как его
отправил бы postgrest-py — columns и Prefer берутся из настоящей pre_upsert (postgrest.base_request_builder), не из догадки.

Семантика PostgREST, которую воспроизводит execute():
  * columns = объединение ключей объектов пачки; объекту без колонки достаётся null (default_to_null=True, умолчание клиента);
  * resolution=merge-duplicates — у существующего ключа обновляются ровно колонки columns, у нового — вставка;
  * resolution=ignore-duplicates — существующий ключ не трогается (ON CONFLICT DO NOTHING), новый — вставка;
  * return=representation — в ответе только реально вставленные / обновлённые строки.
delete / update / insert без upsert — записываются в requests и бросают AssertionError: в этих путях их быть не должно.
Сеть не используется.
"""
import copy
from collections import defaultdict

from postgrest.base_request_builder import pre_upsert

KEYS = {
    "ozon_posting_items": ("posting_number", "sku"),
    "pipeline_runtime_state": ("state_key",),
    "marketplace_orders": ("order_date", "marketplace_code", "marketplace_sku", "order_schema"),
}


class Result:
    def __init__(self, data):
        self.data = data


class FakePostgrest:
    def __init__(self):
        self.tables = defaultdict(dict)       # таблица → {ключ: строка}
        self.requests = []                    # (таблица, метод, columns | None, resolution | None, [наборы ключей объектов])
        self.fail_on_upsert = {}              # номер upsert-запроса (с 1) → исключение

    def key_of(self, table, row):
        return tuple(str(row.get(c)) for c in KEYS[table])

    def seed(self, table, rows):
        for r in rows:
            self.tables[table][self.key_of(table, r)] = dict(r)

    def rows(self, table):
        return [dict(r) for r in self.tables[table].values()]

    def get(self, table, **key):
        return self.tables[table].get(tuple(str(key[c]) for c in KEYS[table]))

    def upserts(self, table=None):
        return [r for r in self.requests if r[1] == "upsert" and (table is None or r[0] == table)]

    def table(self, name):
        return _Builder(self, name)


class _Builder:
    def __init__(self, pg, name):
        self.pg, self.name = pg, name
        self.op, self.filters, self.orders, self.rng, self.lim = None, [], [], None, None

    # --- чтение
    def select(self, cols="*"):
        self.op = ("select", cols)
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def gt(self, col, val):
        self.filters.append(("gt", col, val))
        return self

    def gte(self, col, val):
        self.filters.append(("gte", col, val))
        return self

    def lte(self, col, val):
        self.filters.append(("lte", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def order(self, col, desc=False):
        self.orders.append(col)
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def limit(self, n):
        self.lim = n
        return self

    # --- запись
    def upsert(self, json, on_conflict="", ignore_duplicates=False, returning="representation", default_to_null=True, count=None):
        args = pre_upsert(json, count=count, returning=returning, ignore_duplicates=ignore_duplicates, on_conflict=on_conflict,
                          default_to_null=default_to_null)
        self.op = ("upsert", args, copy.deepcopy(json))
        return self

    def delete(self, *a, **k):
        self.op = ("delete",)
        return self

    def update(self, *a, **k):
        self.op = ("update",)
        return self

    def insert(self, *a, **k):
        self.op = ("insert",)
        return self

    def _match(self, row):
        for kind, col, val in self.filters:
            v = row.get(col)
            if kind == "eq" and str(v) != str(val):
                return False
            if kind == "gt" and not str(v) > str(val):
                return False
            if kind == "gte" and not str(v) >= str(val):
                return False
            if kind == "lte" and not str(v) <= str(val):
                return False
            if kind == "in" and str(v) not in {str(x) for x in val}:
                return False
        return True

    def execute(self):
        pg, kind = self.pg, self.op[0]
        if kind in ("delete", "update", "insert"):
            pg.requests.append((self.name, kind, None, None, []))
            raise AssertionError(f"{kind} на {self.name} — в этом пути запрещено")
        if kind == "select":
            rows = [r for r in pg.tables[self.name].values() if self._match(r)]
            for col in reversed(self.orders):
                rows.sort(key=lambda r, c=col: str(r.get(c)))
            if self.rng:
                rows = rows[self.rng[0]:self.rng[1] + 1]
            if self.lim is not None:
                rows = rows[:self.lim]
            cols = self.op[1]
            if cols and cols != "*":
                names = [c.strip() for c in cols.split(",")]
                rows = [{c: r.get(c) for c in names} for r in rows]
            return Result([dict(r) for r in rows])
        _, args, json = self.op
        params = dict(args.params)
        columns = [c.strip('"') for c in params["columns"].split(",")]
        prefer = args.headers.get("prefer")
        resolution = "ignore" if "resolution=ignore-duplicates" in prefer else "merge"
        n = len(pg.upserts()) + 1
        pg.requests.append((self.name, "upsert", frozenset(columns), resolution, [frozenset(o) for o in json]))
        if n in pg.fail_on_upsert:
            raise pg.fail_on_upsert[n]
        out = []
        table = pg.tables[self.name]
        for obj in json:
            vals = {c: obj.get(c) for c in columns}            # нет колонки у объекта — null, как default_to_null=True
            key = pg.key_of(self.name, vals)
            if key in table:
                if resolution == "ignore":
                    continue
                table[key].update(vals)
                out.append(dict(table[key]))
            else:
                table[key] = dict(vals)
                out.append(dict(vals))
        return Result(out if "return=representation" in prefer else [])
