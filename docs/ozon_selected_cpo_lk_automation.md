# Ozon Selected CPO LK Automation

## Purpose

If Ozon confirms that `selected products CPO` is not available through official API, the fallback is automated LK export plus importer, not a manual daily XLSX process.

## Proposed architecture

1. dedicated service LK account
2. secure cookie/session storage
3. browser automation flow (Playwright or equivalent)
4. select D-1 period
5. navigate to:
   - `Оплата за заказ -> Выбранные товары`
   - or relevant `Аналитика продвижения` section
6. export XLSX/CSV
7. store file in controlled storage
8. run importer
9. compare imported total with finance reconciliation layer
10. send alert on failure

## Operational pieces

- session storage must be encrypted/protected
- exporter should save deterministic file metadata:
  - report date
  - source account
  - file hash
  - timestamp
- importer should be idempotent by file hash
- reconciliation should compare:
  - imported selected CPO
  - Performance API all-products CPO
  - CPC
  - finance total advertising

## Risks

- 2FA
- captcha
- UI changes
- expired session/cookies
- access rights in LK
- hidden filters or different account context

## Telegram / alerting

Automation should alert when:
- export fails
- file structure changes
- total spend deviates unexpectedly
- reconciliation gap exceeds threshold

## When Playwright is NOT needed

Playwright / browser automation is a fallback path, not the preferred primary path.

Do **not** invest in LK automation if Ozon support confirms one of these:

1. `statistics/json` supports `SEARCH_PROMO / CPO` campaigns like `Оплата за заказ: выбранные товары`;
2. there is a separate official Performance API endpoint/report for selected CPO;
3. there is another official API path for selected CPO.

In that case:
- this LK automation block becomes unnecessary;
- the primary path should be an API loader;
- XLSX importer stays only as backup or diagnostic tooling.

Playwright should move from design to implementation only if Ozon support explicitly confirms:
- there is no API for selected CPO;
- `statistics/json` is unavailable for `SEARCH_PROMO / CPO`;
- the only source is LK export.

## Decision order

1. wait for Ozon support response
2. if API exists -> build API loader
3. if API does not exist -> use Playwright + XLSX importer
4. before support response -> keep Playwright only as documented fallback
