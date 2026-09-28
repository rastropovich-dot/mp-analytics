#!/bin/zsh
# Ночная сборка книги «Фин рез» на машине владельца (сорок третья §5): launchd в 06:00 МСК, когда ночь Render уже кончилась
# (~05:10 МСК). Без --excel-check — ночью Excel не открываем, у владельца могут быть открыты книги. Лог — logs/finrez_nightly/,
# копия книги — в FINREZ_COPY_DIR (без переменной шаг копии пропускается с записью в лог), итог сборки — строкой в
# pipeline_runtime_state (finrez_nightly:last) для утреннего алерта 07:30 UTC; FINREZ_NO_STATUS=1 — итог не писать (ручной прогон).
#
#     scripts/finrez_nightly.sh                      обычный запуск (как из launchd)
#     FINREZ_NO_STATUS=1 scripts/finrez_nightly.sh   ручная проверка: книга, лог, копия — без записи итога в базу (db_writes = 0)
#
# Окно книги: с FINREZ_MONTH_FROM (по умолчанию 2026-04) по вчера; приёмка — последний месяц, дней min(21, число вчера).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 2
LOGDIR="$ROOT/logs/finrez_nightly"
mkdir -p "$LOGDIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOGDIR/finrez_$STAMP.log"
exec > >(tee -a "$LOG") 2>&1
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
T0=$(date +%s)
YESTERDAY="$(date -v-1d +%Y-%m-%d)"
MONTH_FROM="${FINREZ_MONTH_FROM:-2026-04}"
MONTH_TO="${YESTERDAY:0:7}"
DAY="${YESTERDAY:8:2}"
CHECK_DAYS=$(( ${DAY#0} < 21 ? ${DAY#0} : 21 ))
OUT="data/reports/finrez_${MONTH_FROM}_${MONTH_TO}.xlsx"
BOOK="data/reports/ozon_${MONTH_TO}_to_${YESTERDAY}.xlsx"
echo "finrez_nightly: старт $STARTED, окно $MONTH_FROM … $YESTERDAY, книга → $OUT, лог $LOG"
ARGS=(--month-from "$MONTH_FROM" --month-to "$MONTH_TO" --date-to "$YESTERDAY" --check --check-month "$MONTH_TO" --check-days "$CHECK_DAYS" --out "$OUT")
if [ -f "$BOOK" ]; then ARGS+=(--book "$BOOK"); else echo "утренней книги $BOOK на диске нет — сверка заказов с ней пропущена"; fi
venv/bin/python3 scripts/report_finrez.py "${ARGS[@]}"
RC=$?
SECONDS_TOTAL=$(( $(date +%s) - T0 ))
ERROR=""
if [ "$RC" -ne 0 ]; then ERROR="report_finrez.py завершился с кодом $RC"; fi
if [ ! -f "$OUT" ]; then ERROR="${ERROR:+$ERROR; }книги $OUT нет"; fi
SIZE=""; SHA=""
if [ -f "$OUT" ]; then
  SIZE=$(stat -f %z "$OUT")
  SHA=$(shasum -a 256 "$OUT" | cut -d' ' -f1)
  echo "книга: $OUT — $SIZE байт, sha256 $SHA, $SECONDS_TOTAL с"
fi
COPIED=""
if [ -n "${FINREZ_COPY_DIR:-}" ]; then
  if [ -f "$OUT" ] && [ -d "$FINREZ_COPY_DIR" ]; then
    if cp "$OUT" "$FINREZ_COPY_DIR/"; then COPIED="$FINREZ_COPY_DIR/$(basename "$OUT")"; echo "копия → $COPIED"; else ERROR="${ERROR:+$ERROR; }копия в $FINREZ_COPY_DIR не удалась"; fi
  else
    ERROR="${ERROR:+$ERROR; }копия пропущена: нет книги или каталога $FINREZ_COPY_DIR"
  fi
else
  echo "FINREZ_COPY_DIR не задан — копия владельцу пропущена"
fi
if [ -n "${FINREZ_NO_STATUS:-}" ]; then
  echo "FINREZ_NO_STATUS: итог в базу не пишу (db_writes = 0)"
  venv/bin/python3 scripts/finrez_nightly_status.py --print --date "$YESTERDAY" --started "$STARTED" --seconds "$SECONDS_TOTAL" --out "$OUT" --bytes "${SIZE:-0}" --sha256 "${SHA:-}" --rc "$RC" --copied "${COPIED:-}" --error "${ERROR:-}" --log "$LOG"
else
  venv/bin/python3 scripts/finrez_nightly_status.py --write --date "$YESTERDAY" --started "$STARTED" --seconds "$SECONDS_TOTAL" --out "$OUT" --bytes "${SIZE:-0}" --sha256 "${SHA:-}" --rc "$RC" --copied "${COPIED:-}" --error "${ERROR:-}" --log "$LOG"
fi
echo "finrez_nightly: конец $(date -u +%Y-%m-%dT%H:%M:%SZ), код $RC${ERROR:+, ошибка: $ERROR}"
exit "$RC"
