{{ config(materialized='table',
          indexes=[{'columns': ['snapshot_date', 'rooms_count']}],
          post_hook='analyze {{ this }}') }}

with obs as (
    select ob.snapshot_date as d, ob.offer_id, ob.price, o.rooms_count
    from {{ source('core', 'offer_observation') }} ob
    join {{ source('core', 'offer') }} o using (source, offer_id)
    where ob.source = 'cian' and o.rooms_count in (1, 2)
      and not ob.is_daily and ob.price > 0
      -- Последний ЗАВЕРШЁННЫЙ обход: сегодняшний день уже есть в
      -- наблюдениях, но собран наполовину, и медиана по нему показала бы
      -- не рынок, а то, до какой корзины успел дойти обход.
      and ob.snapshot_date <= (select max(snapshot_date) from {{ source('raw', 'collection_run') }}
                               where source = 'cian' and status = 'ok')),
-- Пары только соседних дней: разрыв в сутки и разрыв в неделю — разные
-- величины, в один дневной шаг их складывать нельзя.
pairs as (
    select d, rooms_count, lag(d) over w as prev_d,
           ln(price / lag(price) over w) as g
    from obs window w as (partition by offer_id order by d)),
agg as (
    select d, rooms_count::smallint as rooms_count, count(*) as offers,
           percentile_cont(0.5) within group (order by price) as med_price
    from obs group by 1, 2
    union all
    select d, 0::smallint, count(*),
           percentile_cont(0.5) within group (order by price)
    from obs group by 1),
step as (
    select d, rooms_count::smallint as rooms_count, sum(g) as sum_ln,
           count(*) as pairs
    from pairs where g is not null and prev_d = d - 1 group by 1, 2
    union all
    select d, 0::smallint, sum(g), count(*)
    from pairs where g is not null and prev_d = d - 1 group by 1)
select a.d                as snapshot_date
     , a.rooms_count
     , a.offers
     , a.med_price
     , s.sum_ln
     , coalesce(s.pairs, 0) as pairs
from agg a left join step s on s.d = a.d and s.rooms_count = a.rooms_count
