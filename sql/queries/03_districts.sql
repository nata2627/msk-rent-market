\pset border 2
\echo ''
\echo '=== РАЙОНЫ: ГДЕ МОЙ СЕГМЕНТ ДЕШЕВЛЕ ==='
\echo 'Только районы, где есть хотя бы 5 подходящих вариантов.'
\echo ''
select district as район,
       count(*) as вариантов,
       percentile_cont(0.5) within group (order by price)::int as медиана,
       min(price)::int as минимум,
       percentile_cont(0.5) within group (order by large_park_distance_m)::int as "парк,м",
       percentile_cont(0.5) within group (order by metro_walk_min)::int as "метро,мин"
from marts.offer_segment
where source = 'cian' and is_my_segment and district is not null and price is not null
group by district having count(*) >= 5
order by 3
limit 30;
