\pset border 2
\echo ''
\echo '=== РАЙОНЫ: ГДЕ ДЕШЕВЛЕ ==='
\echo 'Только районы, где есть хотя бы 5 объявлений о длительной аренде.'
\echo 'Своих критериев у запроса нет — см. 01_my_flats.sql.'
\echo ''
select district as район,
       count(*) as вариантов,
       percentile_cont(0.5) within group (order by price)::int as медиана,
       percentile_cont(0.5) within group (order by price / total_area)::int as "за м2",
       min(price)::int as минимум,
       percentile_cont(0.5) within group (order by large_park_distance_m)::int as "парк,м",
       percentile_cont(0.5) within group (order by metro_real_walk_min)::int as "метро,мин"
from marts.offer_scored
where source = 'cian' and district is not null and price is not null and total_area > 0
  and not coalesce(is_duplicate, false)
  and not coalesce(is_room_share, false)
  and not coalesce(is_daily, false)
  and coalesce(lease_term_type, 'longTerm') = 'longTerm'
group by district having count(*) >= 5
order by 3
limit 30;
