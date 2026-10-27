{{ config(materialized='table', post_hook='analyze {{ this }}') }}

-- ---------------------------------------------------------------------
-- 2026-10-27. Качество сбора по дням, для публичной витрины.
--
-- Вопрос читателя витрины: можно ли верить числам, если площадка отдает
-- данные рывками. Ответ даем числами за каждый день, а не словами.
--
-- Почему день целиком, а не по комнатностям. Полный обход делит Москву
-- по районам и цене без комнатности (rooms пустой), узкий срез делит по
-- комнатностям. Корзины разных режимов по комнатностям не сопоставить,
-- а день сопоставим всегда.
--
-- Корзины берутся только из успешных прогонов, как в покрытии дня
-- (box_report.sh): упавший прогон закрыт следующим, и его недобор
-- данные не портит. Лист дерева дробления это корзина, которая сама
-- собирала (not was_split). Заявленное ЦИАНом число неточное и бывает
-- меньше собранного, поэтому полнота считается по least(собрано,
-- заявлено): перебор одной корзины не прячет недобор другой.
--
-- Дни без единого успешного прогона тоже строки: пропущенный день
-- должен быть виден, а не выпасть из ряда.
-- ---------------------------------------------------------------------
with runs as (
    select snapshot_date,
           count(*)                                  as runs,
           count(*) filter (where status = 'ok')     as runs_ok,
           count(*) filter (where status in ('failed', 'aborted')) as runs_failed,
           string_agg(distinct mode, ',' order by mode)
               filter (where status = 'ok')          as modes_ok
      from {{ source('raw', 'collection_run') }}
     where source = 'cian' and status <> 'running'
     group by 1
),
leaves as (
    select r.snapshot_date,
           count(*)                                                   as buckets,
           count(*) filter (where b.error is not null)                as buckets_error,
           count(*) filter (where b.error is null
                              and b.collected_count < b.claimed_count) as buckets_underfilled,
           count(*) filter (where b.is_truncated)                     as buckets_truncated,
           sum(b.claimed_count) filter (where b.error is null)        as claimed,
           sum(least(b.collected_count, b.claimed_count))
               filter (where b.error is null)                         as collected
      from {{ source('raw', 'collection_run') }} r
      join {{ source('raw', 'bucket') }} b using (run_id)
     where r.source = 'cian' and r.status = 'ok' and not b.was_split
     group by 1
),
-- Однушки и двушки, увиденные за день: это сегмент всех выводов панели.
-- Число из panel_day, чтобы совпадать с индексом на той же витрине.
seg as (
    select snapshot_date, offers as offers_12
      from {{ ref('panel_day') }}
     where rooms_count = 0
),
days as (
    select generate_series(min(snapshot_date), max(snapshot_date), interval '1 day')::date
               as snapshot_date
      from runs
)
select d.snapshot_date,
       coalesce(r.runs, 0)                          as runs,
       coalesce(r.runs_ok, 0)                       as runs_ok,
       coalesce(r.runs_failed, 0)                   as runs_failed,
       r.modes_ok,
       coalesce(r.runs_ok, 0) > 0                   as is_covered,
       coalesce(l.buckets, 0)                       as buckets,
       coalesce(l.buckets_error, 0)                 as buckets_error,
       coalesce(l.buckets_underfilled, 0)           as buckets_underfilled,
       coalesce(l.buckets_truncated, 0)             as buckets_truncated,
       l.claimed,
       l.collected,
       round(100.0 * l.collected / nullif(l.claimed, 0), 2)  as completeness_pct,
       round(100.0 * l.buckets_underfilled / nullif(l.buckets - l.buckets_error, 0), 1)
                                                    as underfilled_pct,
       s.offers_12
  from days d
  left join runs   r using (snapshot_date)
  left join leaves l using (snapshot_date)
  left join seg    s using (snapshot_date)
