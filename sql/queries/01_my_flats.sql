\pset border 2
\echo ''
\echo '=== ЛУЧШИЕ ВАРИАНТЫ В МОЁМ СЕГМЕНТЕ ==='
\echo '1-2 комнаты, от 40 кв.м, животные можно, длительно, не апартаменты,'
\echo 'крупный парк в пределах километра. Сортировка по цене.'
\echo ''
select district as район,
       rooms_count as комн,
       total_area::int as "м2",
       price::int as "цена/мес",
       coalesce(deposit_rub::int, 0) as залог,
       coalesce(client_fee_pct::int, 0) as "комис,%",
       large_park_distance_m as "парк,м",
       metro_walk_min as "метро,мин",
       floor || '/' || floors_total as этаж,
       case pets_state when 'allowed' then 'да' else 'по догов.' end as животные,
       split_part(url, '?', 1) as ссылка
from marts.offer_segment
where source = 'cian' and is_my_segment and is_near_park
  and price is not null
order by price
limit 25;
