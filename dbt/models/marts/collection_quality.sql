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
--
-- 2026-11-10. Вторая мерка, полнота дня по сегменту: однушки и двушки,
-- увиденные за день, против заявленного ЦИАНом в верхних корзинах
-- (комнатность без района, цены и фильтра животных). Мерка по корзинам
-- выше берет только успешные прогоны и молчит о дне, где прогон
-- прервали: 29.10 и 03.11 у нее пусты и из медианы просто выпадали. Эта
-- мерка считает все прогоны дня и такой день видит. Верхнее число
-- берется максимумом за день и только как ориентир, поэтому доля
-- ограничена сотней. Если до верхней корзины комнатности в тот день не
-- дошли (03.11 двушки не начинались), берется ее последнее заявленное
-- число до этого дня: иначе несобранная комнатность выпала бы и из
-- собранного, и из заявленного, и день выглядел бы полным.
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
days as (
    select generate_series(min(snapshot_date), max(snapshot_date), interval '1 day')::date
               as snapshot_date
      from runs
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
-- Для полноты дня собранное считается по raw, как в боте и вечернем
-- пульсе (scripts/box_seg.sh): panel_day обрезана последним успешным
-- прогоном, и упавший последний день выглядел бы пустым.
got as (
    select s.snapshot_date,
           count(distinct s.offer_id) filter (where o.rooms_count = 1) as got_1,
           count(distinct s.offer_id) filter (where o.rooms_count = 2) as got_2
      from {{ source('raw', 'offer_snapshot') }} s
      join {{ source('core', 'offer') }} o using (source, offer_id)
     where s.source = 'cian' and o.rooms_count in (1, 2)
     group by 1
),
top_day as (
    select snapshot_date,
           max(b.claimed_count) filter (where b.rooms = '1') as claimed_1,
           max(b.claimed_count) filter (where b.rooms = '2') as claimed_2
      from {{ source('raw', 'collection_run') }} r
      join {{ source('raw', 'bucket') }} b using (run_id)
     where r.source = 'cian' and b.depth = 0 and b.rooms in ('1', '2')
       and not b.pets_filter and b.district is null and b.okrug is null
       and b.price_min is null and b.price_max is null
     group by 1
),
top as (
    select d.snapshot_date,
           t.claimed_1 is null or t.claimed_2 is null as claimed_carried,
           coalesce(t.claimed_1, (select p.claimed_1 from top_day p
                                   where p.snapshot_date < d.snapshot_date and p.claimed_1 is not null
                                   order by p.snapshot_date desc limit 1)) as claimed_1,
           coalesce(t.claimed_2, (select p.claimed_2 from top_day p
                                   where p.snapshot_date < d.snapshot_date and p.claimed_2 is not null
                                   order by p.snapshot_date desc limit 1)) as claimed_2
      from days d
      left join top_day t using (snapshot_date)
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
       s.offers_12,
       t.claimed_1 + t.claimed_2                    as claimed_12,
       t.claimed_carried,
       coalesce(g.got_1, 0) + coalesce(g.got_2, 0)  as got_12,
       -- Перебор одной комнатности не прячет недобор другой, как и у
       -- корзин выше: каждая ограничена своим заявленным.
       {{ capped_pct('least(coalesce(g.got_1, 0), t.claimed_1) + least(coalesce(g.got_2, 0), t.claimed_2)',
                     't.claimed_1 + t.claimed_2') }} as seg_completeness_pct,
       {{ capped_pct('coalesce(g.got_1, 0)', 't.claimed_1') }} as seg_1_pct,
       {{ capped_pct('coalesce(g.got_2, 0)', 't.claimed_2') }} as seg_2_pct
  from days d
  left join runs   r using (snapshot_date)
  left join leaves l using (snapshot_date)
  left join seg    s using (snapshot_date)
  left join top    t using (snapshot_date)
  left join got    g using (snapshot_date)
