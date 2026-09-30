"""Правила денег WB — одно место для книги «Фин рез» (scripts/report_finrez_wb.py), листа «WB - месяц» и утренней книги
(scripts/report_wb_month.py, scripts/book_wb_sheet.py) и сверок (WB-14 §2–§4, решения владельца 2026-09-28).

1. Удержания отчёта реализации (строки «Удержание», поле deduction) — по виду bonus_type_name, classify_deduction:
   * «Оказание услуг «WB Продвижение», документ №…» → ADS_WITHHELD: реклама, оплаченная с баланса, — те же деньги, что
     списания adv/v1/upd с payment_type «Баланс» (тождество 02.04 … 31.08: 12 683 941,00 = 12 683 941,00, WB-13). В «Прочее»
     не входит: реклама считается один раз, из upd.
   * «Аванс за услугу "Баллы за отзывы"» → REVIEW_ADVANCE, «Возврат неиспользованного остатка аванса …» → REVIEW_REFUND:
     предоплата и её возврат. В «Прочее» идёт только нетто — в день возврата (review_advance_net): 30.06 аванс
     2 298 480 + 4 172 400, 22.07 возврат 2 258 220 + 4 172 400 → 40 260 расхода 22.07.
   * всё остальное → OTHER: входит в «Прочее» (Джем, Витрина Магазина, Списание за отзыв …). Виды не из KNOWN_OTHER_PREFIXES
     печатаются списком с суммами (unknown_deductions) — новый вид удержаний не пройдёт незамеченным.
   Сопоставление — по началу строки: номера документов и отзывов меняются.
2. Реклама — только payment_type «Баланс» (is_balance_payment); реклама за кэшбэк WB — не наши деньги, справочно.
3. Упаковка — предмет «Упаковки для украшений» (коробка karatov 75*75*65, nmId 334613161): из строк исключается
   (is_packaging) правилом по предмету, не по одному nmId.
Чистые функции, без ввода-вывода."""
from collections import defaultdict
from decimal import Decimal

Z = Decimal(0)

ADS_WITHHELD = "ads_withheld"
REVIEW_ADVANCE = "review_points_advance"
REVIEW_REFUND = "review_points_refund"
OTHER = "other"
DEDUCTION_KINDS = (ADS_WITHHELD, REVIEW_ADVANCE, REVIEW_REFUND, OTHER)

DEDUCTION_PREFIXES = (
    ("Оказание услуг «WB Продвижение»", ADS_WITHHELD),
    ('Аванс за услугу "Баллы за отзывы"', REVIEW_ADVANCE),
    ('Возврат неиспользованного остатка аванса за услугу "Баллы за отзывы"', REVIEW_REFUND),
)
# Виды «прочих» удержаний, уже виденные в отчёте (апрель … сентябрь 2026): считаются в «Прочее» молча; остальные — вслух.
KNOWN_OTHER_PREFIXES = ("Предоставление услуг по подписке «Джем»", "Витрина Магазина", "Списание за отзыв")
# Фильтр PostgREST для строк аванса и возврата (вся история читается отдельно: аванс июня нужен окну июля).
REVIEW_POINTS_LIKE = "*Баллы за отзывы*"

PACKAGING_SUBJECTS = frozenset({"Упаковки для украшений"})
BALANCE_PAYMENT = "Баланс"


def D(v):
    return v if isinstance(v, Decimal) else (Z if v in (None, "") else Decimal(str(v)))


def classify_deduction(name):
    """Вид удержания по bonus_type_name: ADS_WITHHELD | REVIEW_ADVANCE | REVIEW_REFUND | OTHER (по началу строки)."""
    text = str(name or "").strip()
    for prefix, kind in DEDUCTION_PREFIXES:
        if text.startswith(prefix):
            return kind
    return OTHER


def is_known_other(name):
    text = str(name or "").strip()
    return any(text.startswith(p) for p in KNOWN_OTHER_PREFIXES)


def deduction_label(name):
    """Вид без номера документа / отзыва — для печати сумм по видам."""
    text = str(name or "").strip() or "(пусто)"
    for sep in (", документ №", ": акция", " №"):
        if sep in text:
            text = text.split(sep, 1)[0]
    if text.startswith("Списание за отзыв"):
        return "Списание за отзыв"
    return text


def counted_deduction(row):
    """Часть удержания строки, которая входит в «Прочее»: вид OTHER — целиком, остальные виды — 0."""
    ded = D(row.get("deduction"))
    if not ded:
        return Z
    return ded if classify_deduction(row.get("bonus_type_name")) == OTHER else Z


def other_amount(row):
    """«Ост. расходы и компенсации МП» строки (с НДС, как в отчёте): штрафы + удержания вида OTHER − доплаты."""
    return D(row.get("penalty")) + counted_deduction(row) - D(row.get("additional_payment"))


class DeductionTally:
    """Удержания окна по видам (с НДС) и незнакомые виды «прочего» с суммами — для печати в отчёте."""

    def __init__(self):
        self.by_kind = defaultdict(Decimal)
        self.unknown = defaultdict(Decimal)
        self.rows = defaultdict(int)

    def add(self, row):
        ded = D(row.get("deduction"))
        if not ded:
            return
        kind = classify_deduction(row.get("bonus_type_name"))
        self.by_kind[kind] += ded
        self.rows[kind] += 1
        if kind == OTHER and not is_known_other(row.get("bonus_type_name")):
            self.unknown[deduction_label(row.get("bonus_type_name"))] += ded

    def text(self):
        parts = [f"{k} {self.by_kind.get(k, Z):,.2f} ({self.rows.get(k, 0)} стр.)" for k in DEDUCTION_KINDS]
        unknown = "; незнакомые виды «прочего»: " + (", ".join(f"«{n}» {v:,.2f}" for n, v in sorted(self.unknown.items(), key=lambda kv: -abs(kv[1])))
                                                     if self.unknown else "нет")
        return "удержания по видам: " + ", ".join(parts) + unknown


def review_advance_net(rows, day_fn):
    """Нетто аванса «Баллы за отзывы» по дням возврата: {день: Σ открытых авансов + Σ возвратов дня} и открытый остаток.
    rows — строки удержаний аванса и возврата (вся история), day_fn(row) → день строки. Возврат закрывает аванс: что не
    вернули, израсходовано — расход дня возврата. Аванс без возврата — не расход до возврата (остаток печатается)."""
    events = sorted(((day_fn(r), classify_deduction(r.get("bonus_type_name")), D(r.get("deduction")), str(r.get("rrd_id") or ""))
                     for r in rows if classify_deduction(r.get("bonus_type_name")) in (REVIEW_ADVANCE, REVIEW_REFUND)), key=lambda e: (e[0], e[3]))
    outstanding, net = Z, {}
    refunds_by_day = defaultdict(Decimal)
    advances_before = []
    for day, kind, amount, _rid in events:
        if kind == REVIEW_ADVANCE:
            advances_before.append((day, amount))
        else:
            refunds_by_day[day] += amount
    for day in sorted(set(d for d, *_ in events)):
        outstanding += sum((a for d, a in advances_before if d == day), Z)
        if day in refunds_by_day:
            net[day] = outstanding + refunds_by_day[day]
            outstanding = Z
    return net, outstanding


def is_packaging(subject_name):
    return str(subject_name or "").strip() in PACKAGING_SUBJECTS


def is_balance_payment(payment_type):
    return str(payment_type or "").strip() == BALANCE_PAYMENT
