# Ozon Performance Limits and Production Schedule

## Confirmed Ozon Performance limits

- Ozon confirmed the `statistics/json` export limit is `2000 campaigns/day`.
- One campaign consumes one unit of that daily export limit.
- A batch of `10` campaigns consumes `10 campaign units`.
- The general API limit `100000` does not apply to `statistics/json` export jobs.
- The `statistics/json` export limit cannot be increased.
- There is no alternative endpoint that bypasses this export limit for CPC statistics.

## Production schedule

Moscow is `UTC+3`, without DST.

- Daily load: `06:10 MSK` = `03:10 UTC`
  - Render cron: `10 3 * * *`
- Telegram executive report: `09:00 MSK` = `06:00 UTC`
  - Render cron: `0 6 * * *`

## Why daily-yesterday beats a rolling 30-day window

- Executive and pricing decisions are made on the freshest complete day, not on a wide historical CPC window.
- Ozon counts `statistics/json` quota in campaign units, so a daily D-1 run is predictable:
  - current campaign count is about `785`;
  - full D-1 CPC fits into the confirmed `2000 campaigns/day` quota;
  - the production budget keeps a reserve and targets at most `1800` campaign units per daily run.
- Historical windows are still useful, but they belong to bounded backfill jobs, not to the daily production cron.

## Production Ozon Performance mode

- `run_daily_pipeline.py --skip-telegram` now calls Ozon Performance in `daily-yesterday` mode.
- For a morning run on `2026-05-06 MSK`, the target Ozon Performance window is:
  - `2026-05-05..2026-05-05`
- Daily D-1 CPC selection is now completeness-first by default:
  - keep all CPC campaigns whose campaign dates overlap the target date;
  - do not exclude a campaign only because it is currently inactive or not updated in that day;
  - use `recent` mode only as an explicit fallback, because it may miss D-1 spend needed for management decisions.
- Historical and recovery runs stay separate:
  - `--mode full` for explicit historical ranges;
  - `--mode cpc-backfill` for pending CPC batches after the main daily run.
- `cpc-backfill` should resume only canonical D-1 progress created with:
  - `selection_mode = complete`
  - saved `ordered_campaign_ids`
  - saved `campaign_list_hash`
- Legacy CPC progress created before ordered batch persistence should be treated as `legacy partial`:
  - do not resume it automatically;
  - do not assume batch indexes still point to the same campaign set;
  - wait for the next scheduled D-1 run to create a canonical resumable progress key.

## Quota model

- The main scarce resource is not batch count but campaign units:
  - `1 campaign = 1 statistics/json unit`
  - a batch of `10` campaigns consumes `10` units

### Measured against 19 real 429s (2026-09-01)

Ozon help, «Лимиты на запросы»: одна кампания в запросе = одна выгрузка;
лимит проверяется **в начале формирования отчёта**; 2000 выгрузок за 24 часа
на аккаунт и столько же на организацию.

Проверено по ledger на всех 19 отказах `daily_quota_exhausted` в истории —
считался расход на момент прихода 429:

| Метрика | Медиана | Диапазон | Доля от 2000 |
|---|---:|---:|---:|
| `campaign_units` с полуночи UTC | ~1030 | 620 – 1695 | 31 – 85% |
| `campaign_units` скользящие 24ч | ~1070 | 620 – 1705 | 31 – 85% |
| HTTP-запросы | ~103 | 62 – 171 | **3 – 9%** |

**Что установлено твёрдо.** Трактовка «лимит в HTTP-запросах» опровергнута:
отказ «превышен дневной лимит» при 171 запросе из 2000 невозможен. Единица
измерения — выгрузки-кампании, то есть `campaign_units`. Отсюда практическое
следствие: **опросы и скачивания квоту не тратят, и резать частоту опроса
бессмысленно — экономии там нет.** `request_count` в ledger остаётся
вспомогательной метрикой видимости трафика, не мерой расхода квоты.

**Что не установлено.** Наши учтённые units до 2000 не доходят никогда: в
момент отказа мы держим примерно половину лимита. Около половины приходится
не на наш учтённый расход, и по имеющимся данным выбрать причину нельзя.
Кандидатов три, ни один пока не подтверждён и не отвергнут:

1. **Сторонний потребитель в организации** — лимит общий на организацию,
   а ledger видит только наш аккаунт. Косвенный довод: отказы 2026-08-20 в
   20:44 UTC и 2026-08-30 в 17:54 UTC пришли при наших 31% лимита и вне
   нашего ночного окна.
2. **Неучтённые ретраи submit** — ledger пишет строку на submit, но если
   какой-то путь повторяет submit мимо учёта, реальный расход выше нашего.
3. **Погрешность окна** — если окно скользящее и отсчитывается не так, как мы
   считаем, часть расхода попадает в него из предыдущих суток.

Различить их можно только новыми данными: снимок обоих окон в момент 429
уже снимается автоматически (`request_kind = 'quota_snapshot'`, см.
`sql/ozon_statistics_json_usage_last_24h.sql`), и по накоплении нескольких
случаев картина должна проясниться.

Оба порога уже выражены в units, менять их единицу не требуется:

- `--ozon-max-daily-cpc-units 1200` режет `daily_campaign_budget` в units;
- guard recovery-воркера берёт число из `campaign_units` ledger;
  порогов при этом **два**: `used > 1500` жёстко зашит для pre-фазы
  (`scripts/ozon_performance_recovery_worker.py:98`), а post-фаза уходит от
  `daily_limit - reserve` = `2000 - 200` = `1800`.
- Production env defaults:
  - `OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_LIMIT=2000`
  - `OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_RESERVE=200`
  - `OZON_PERFORMANCE_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN=1800`
  - `OZON_PERFORMANCE_DAILY_CPC_SELECTION_MODE=complete`
- Confirmed planning dry-run example for `2026-05-05`:
  - `raw_campaign_count = 948`
  - `filtered_recent_count = 185`
  - `raw_cpc_count = 948`
  - `date_overlap_cpc_count = 899`
  - `selected_cpc_count = 899`
  - `excluded_by_recent_filter_count = 714`
  - `cpc_campaign_count = 899`
  - `batch_size = 10`
  - `total_batches = 90`
  - `campaign_units = 899`
  - `usable_limit = 1800`
  - `would_fit_daily_limit = yes`
- This confirms the production D-1 CPC path is low-risk by quota:
  - rolling 30-day daily mode is removed;
  - the morning daily load now works on one day only;
  - the D-1 CPC run leaves a quota buffer of `901 campaign units`.
- If daily CPC cannot fit into the remaining budget:
  - CPC status becomes `pending_quota`
  - overall run status becomes `partial_quota`
  - remaining campaigns stay in DB-backed progress and can be retried by bounded backfill later
- `--plan-only` prints a safe planning summary before any report jobs are created:
  - `target_date`
  - `raw_campaign_count`
  - `raw_cpc_count`
  - `filtered_recent_count`
  - `date_overlap_cpc_count`
  - `selected_cpc_count`
  - `excluded_by_recent_filter_count`
  - `excluded_by_quota_count`
  - `campaign_units`
  - `daily_limit`
  - `reserve`
  - `usable_limit`
  - `would_fit_daily_limit`
- DB-backed state in `pipeline_runtime_state` remains the source of truth for:
  - `cpc_progress`
  - `statistics/json` job cache
  - `cooldowns`
  - `batch_recommendations`
- For CPC resume safety, `cpc_progress` should contain:
  - `ordered_campaign_ids`
  - `campaign_list_hash`
  - `selection_mode = complete`

## Safe cron split

Current `run_daily_pipeline.py` can now skip the Telegram step.

- Load cron command:

```bash
python3 run_daily_pipeline.py --skip-telegram
```

- Report cron command:

```bash
python3 alerts_telegram.py
```

This split is safer than running one combined cron because:

- the Ozon load can start right after the assumed safe window after the overnight limit reset;
- the executive Telegram report is delayed until the morning, when data loading is expected to be complete;
- a retry or bounded Ozon backfill does not need to resend the executive report.

## Operational guidance

- Do not run manual full pipeline loads during the day unless necessary.
- Prefer `--plan-only` when you need to confirm D-1 campaign units without creating any Ozon report jobs.
- If `daily-yesterday` is forced into `recent` selection mode, treat that as a warning state:
  - it may miss real D-1 CPC spend;
  - it is unsuitable for management decisions that need full yesterday attribution.
- For manual checks, prefer:
  - bounded `cpc-backfill`, or
  - Ozon-only runs with explicit CPC batch limits.
- Before any live `cpc-backfill`, confirm in read-only mode:
  - `ordered_campaign_ids present = yes`
  - `campaign_list_hash present = yes`
  - `selection_mode = complete`
  - `completed_batches / pending_batches`
  - `cpc_status / run_status`
- If resume falls back to `deterministic_sort_fallback`, do not auto-resume that progress.
- Treat `statistics/json` campaign units as the scarce resource.
