{{ config(materialized='table',
          indexes=[{'columns': ['source', 'offer_id']}],
          post_hook='analyze {{ this }}') }}

with panel_start as (
    select min(snapshot_date) as d0
    from {{ source('core', 'offer_observation') }} where source = 'cian'),
-- Последний ЗАВЕРШЁННЫЙ обход, а не последняя дата в панели: сегодняшний
-- день уже есть в наблюдениях, но собран наполовину.
last_ok as (
    select max(snapshot_date) as d1
    from {{ source('raw', 'collection_run') }} where source = 'cian' and status = 'ok'),
seen as (
    select offer_id, count(distinct snapshot_date) as crawls
    from {{ source('core', 'offer_observation') }} where source = 'cian' group by 1)
select s.source
     , s.offer_id
     , s.district
     , s.rooms_count
     , s.total_area
     , s.price                                            as price
     , s.med_price
     , s.published_at::date                               as published_on
     , s.first_seen_date
     , s.last_seen_date
     , s.disappeared_date
     , sn.crawls
     , s.disappeared_date is not null                     as has_left
     , s.disappeared_date - s.published_at::date          as age_at_exit
     , ln(s.price / s.med_price)                          as log_rel_price
     -- Было ли у публикации время не только исчезнуть, но и подтвердить
     -- исчезновение. Без этой отметки последний день ряда всегда провален:
     -- 03.10.2026 показывал 1,5% быстрых уходов против 7-10% в остальные
     -- дни — не потому, что рынок встал, а потому, что подтвердиться они
     -- не успели.
     --
     -- Четыре дня, и каждый за своё. Объявление, опубликованное после
     -- десяти утра, попадает в выдачу только следующим обходом — это
     -- один день. Снятие подтверждается тремя СОБРАННЫМИ днями отсутствия
     -- (DISAPPEARED_AFTER_DAYS = 3) — это ещё три. Порог посчитан от
     -- константы сбора, а не назначен на глаз.
     , s.published_at::date <= l.d1 - 4                   as exit_observable
     -- Границы групп взяты для читаемости таблицы, а не выведены из
     -- данных. Вывод от них не зависит: рядом на графике тот же разрез по
     -- каждому обходу отдельно, без всяких границ.
     , case when s.disappeared_date is null then 5
            when sn.crawls = 1 then 1
            when sn.crawls <= 3 then 2
            when sn.crawls <= 7 then 3
            else 4 end                                    as speed_order
     , case when s.disappeared_date is null then 'ещё висит'
            when sn.crawls = 1  then 'ушло за один обход'
            when sn.crawls <= 3 then 'ушло за 2-3 обхода'
            when sn.crawls <= 7 then 'ушло за 4-7 обходов'
            else 'ушло за 8 и больше обходов' end          as speed_group
     , case when coalesce(s.is_duplicate, false)   then 'дубль'
            when coalesce(s.is_room_share, false)  then 'комната, а не квартира'
            when coalesce(s.is_price_outlier, false) then 'выброс по цене'
            when s.price / s.med_price < 0.2 or s.price / s.med_price > 5
                 then 'отношение к сопоставимым вне 0,2-5'
            end                                           as exclude_reason
from {{ ref('offer_scored') }} s
cross join panel_start p
cross join last_ok l
join seen sn on sn.offer_id = s.offer_id
where s.source = 'cian'
  and s.rooms_count in (1, 2)
  and s.published_at is not null
  and s.published_at::date >= p.d0
  and s.first_seen_date >= s.published_at::date
  and s.price > 0
  and s.total_area > 0
  and s.med_price > 0
