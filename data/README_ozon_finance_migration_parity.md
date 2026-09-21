# Паритет старого и нового finance API Ozon

## Зачем этот файл

`/v3/finance/transaction/list` и `/v3/finance/transaction/totals` отключаются
**8 сентября 2026** (объявлено 14.07.2026, повторено 24.08.2026). Замена —
`/v1/finance/accrual/by-day`, `/v1/finance/accrual/types`,
`/v1/finance/accrual/postings`.

После отключения проверить, что переход не изменил цифры, будет **нечем**:
старого метода не станет. Поэтому сверка снята заранее и хранится в
репозитории как данные, а не как временный файл.

## Когда и чем снято

```
снято          2026-09-06
код            scripts/ozon_finance_migration_parity.py
период         2026-03-28 … 2026-09-04, 161 дата
метод старый   POST /v3/finance/transaction/list, сумма amount по operation_type
метод новый    POST /v1/finance/accrual/by-day, сумма accrued по type_id
```

Обе стороны считаются **со знаком**: списание отрицательное, возврат
положительный. Модуль не применяется нигде — именно подмена знаковой суммы на
`abs()` дала ложный вывод «новый метод считает нетто, старый брутто», который
опровергнут этой сверкой.

## Что сравнивалось

Тринадцать пар «старое имя операции → новый type_id`:

```
OperationMarketplaceCostPerClick                41  PayPerClick
OperationPromotionWithCostPerOrder              54  Promotion
MarketplaceRedistributionOfAcquiringOperation    1  Acquiring
PremiumMembership                               51  PremiumMembership
InsuranceServiceSellerItem                      76  StockInsurance
OperationMarketplaceAcceleratedProductReviews   96  AcceleratedReviewCollection
StarsMembership                                 74  StarsMembership
OperationMarketplaceServiceStorage              46  Placements
OperationMarketplacePackageMaterialsProvision   38  PackageCost
OperationMarketplacePackageRedistribution       39  PackingFee
TemporaryStorage                                78  TemporaryPlacement
OperationMarketPlaceItemPinReview               61  ReviewsPin
OperationPointsForReviews                       47  PointsForReviews
```

## Результат

**Дат с расхождением: 0 из 161.** По каждому из тринадцати типов сумма за весь
период совпадает до копейки.

```
OperationMarketplaceCostPerClick        −20 515 440,44  =  −20 515 440,44
OperationPromotionWithCostPerOrder      −60 000 567,63  =  −60 000 567,63
MarketplaceRedistributionOfAcquiring     −8 609 931,87  =   −8 609 931,87
PremiumMembership                       −16 758 026,26  =  −16 758 026,26
InsuranceServiceSellerItem               −1 009 969,03  =   −1 009 969,03
AcceleratedProductReviews                  −542 619,40  =     −542 619,40
StarsMembership                            −123 526,98  =     −123 526,98
OperationMarketplaceServiceStorage          −61 559,11  =      −61 559,11
OperationMarketplacePackageMaterialsProv    −41 805,00  =      −41 805,00
OperationMarketplacePackageRedistribution   −83 610,00  =      −83 610,00
TemporaryStorage                           −107 700,00  =     −107 700,00
OperationMarketPlaceItemPinReview            −1 500,00  =       −1 500,00
OperationPointsForReviews                    −1 830,00  =       −1 830,00
```

Следствие для миграции: вопроса «нетто или брутто» не существует, методы
считают одинаково. Переход касается только структуры ответа и разбора типов.

## Формат файла

```json
{
  "built_at": "…",
  "pairs":   {"<старое имя>": <новый type_id>},
  "per_day": {"YYYY-MM-DD": {"<старое имя>": {"old": …, "new": …, "diff": …}}}
}
```
