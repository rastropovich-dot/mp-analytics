#!/usr/bin/env python3
"""Разбор файла детализации отчёта реализации WB (sales-reports/detailed): что в
нём лежит и как складываются колонки ручного листа. Только чтение файла.

Печатает: типы документов и операций, поля дат (orderDt / saleDt / rrDate),
суммы по дням saleDt и по rrDate: оборот (retailAmount продаж − возвратов),
комиссия (ppvzSalesCommission), к перечислению (ppvzForPay), логистика
(deliveryAmount, returnAmount, rebillLogisticCost), эквайринг (acquiringFee),
штрафы, доплаты, хранение, удержания, приёмка.
"""
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal as D

path = sys.argv[1]
data = json.load(open(path), parse_float=D)
print("строк", len(data), "| ключей в строке", len(data[0]) if data else 0)
print("отчёты:", sorted({(r.get("reportId"), r.get("dateFrom"), r.get("dateTo"), r.get("reportType")) for r in data})[:12])
print("docTypeName:", Counter(r.get("docTypeName") for r in data))
print("sellerOperName:", Counter(r.get("sellerOperName") for r in data).most_common(20))
print("bonusTypeName (непустые):", Counter(r.get("bonusTypeName") for r in data if r.get("bonusTypeName")).most_common(10))
print("paymentProcessing:", Counter(r.get("paymentProcessing") for r in data).most_common(5))
print("даты: saleDt", sorted({str(r.get("saleDt"))[:10] for r in data})[:3], "…", sorted({str(r.get("saleDt"))[:10] for r in data})[-3:],
      "| rrDate", sorted({str(r.get("rrDate"))[:10] for r in data}), "| orderDt min/max", min(str(r.get("orderDt")) for r in data)[:10], max(str(r.get("orderDt")) for r in data)[:10])

def d(v):
    return D(str(v)) if v not in (None, "") else D(0)

fields = ["retailAmount", "ppvzSalesCommission", "forPay", "deliveryAmount", "returnAmount", "rebillLogisticCost",
          "acquiringFee", "penalty", "additionalPayment", "paidStorage", "deduction", "paidAcceptance", "ppvzReward",
          "vw", "vwNds", "retailPriceWithDisc", "quantity", "cashbackAmount", "cashbackDiscount", "cashbackCommissionChange"]
tot = {f: D(0) for f in fields}
for r in data:
    for f in fields:
        tot[f] += d(r.get(f))
print("ИТОГО по всем строкам:", {f: str(v) for f, v in tot.items() if v})

def day_table(key):
    by = defaultdict(lambda: defaultdict(D))
    for r in data:
        day = str(r.get(key))[:10]
        sign = -1 if r.get("docTypeName") == "Возврат" else 1
        oper = r.get("sellerOperName") or ""
        by[day]["строк"] += 1
        if oper in ("Продажа", "Возврат"):
            by[day]["оборот±"] += sign * d(r.get("retailAmount"))
            by[day]["комиссия±"] += sign * d(r.get("ppvzSalesCommission"))
            by[day]["к_перечислению±"] += sign * d(r.get("forPay"))
        by[day]["логистика"] += d(r.get("deliveryAmount"))
        by[day]["логистика_возврат"] += d(r.get("returnAmount"))
        by[day]["эквайринг"] += d(r.get("acquiringFee"))
        by[day]["штраф"] += d(r.get("penalty"))
        by[day]["доплата"] += d(r.get("additionalPayment"))
        by[day]["хранение"] += d(r.get("paidStorage"))
        by[day]["удержания"] += d(r.get("deduction"))
        by[day]["приёмка"] += d(r.get("paidAcceptance"))
        by[day]["vw"] += d(r.get("vw"))
        by[day]["vwNds"] += d(r.get("vwNds"))
        by[day]["oper:" + oper] += 1
    print(f"\n=== по дням {key}")
    for day in sorted(by):
        row = by[day]
        print(day, "| строк", row["строк"], "| оборот±", row["оборот±"], "| комиссия±", row["комиссия±"], "| к перечислению±", row["к_перечислению±"],
              "| логистика", row["логистика"], "+возврат", row["логистика_возврат"], "| эквайринг", row["эквайринг"], "| штраф", row["штраф"],
              "| доплата", row["доплата"], "| хранение", row["хранение"], "| удержания", row["удержания"], "| приёмка", row["приёмка"],
              "| vw", row["vw"], "vwNds", row["vwNds"])
    return by

day_table("saleDt")
day_table("rrDate")
# первые строки — для глаз
for r in data[:2]:
    print({k: v for k, v in r.items() if v not in (None, "", 0, "0")})
