-- Правдоподобие цен. Ничего не удаляем: подозрительное помечаем и смотрим
-- глазами. Отсечение по цене на этапе загрузки сместило бы выборку по той
-- самой переменной, которую мы объясняем.
\echo 'Распределение цен длительной аренды на последнюю дату'
select percentile_cont(0.01) within group (order by price)::int as p01,
       percentile_cont(0.25) within group (order by price)::int as p25,
       percentile_cont(0.50) within group (order by price)::int as median,
       percentile_cont(0.75) within group (order by price)::int as p75,
       percentile_cont(0.99) within group (order by price)::int as p99,
       min(price)::int as min_price, max(price)::int as max_price, count(*)
from core.offer_observation
where snapshot_date = (select max(snapshot_date) from core.offer_observation)
  and not is_daily and price is not null;

\echo 'Подозрительно дёшево для Москвы (меньше 15 тыс. в месяц)'
select count(*) from core.offer_observation
where not is_daily and price < 15000
  and snapshot_date = (select max(snapshot_date) from core.offer_observation);

\echo 'Цена за квадратный метр вне правдоподобного диапазона'
select count(*) as suspicious_price_per_sqm
from core.offer_observation ob
join core.offer o on o.source = ob.source and o.offer_id = ob.offer_id
where not ob.is_daily and o.total_area > 0
  and ob.price / o.total_area not between 300 and 20000;
