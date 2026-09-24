\pset border 2
\echo ''
\echo '=== ПОДЕШЕВЕВШИЕ ОБЪЯВЛЕНИЯ ==='
\echo 'Нужно минимум два дня наблюдений. Если пусто — значит снимок пока один.'
\echo ''
select o.district as район, o.rooms_count as комн, o.total_area::int as "м2",
       pc.prev_price::int as было, pc.new_price::int as стало,
       pc.delta_pct as "%", pc.days_between_observations as дней,
       pc.changed_on as когда, split_part(o.url, '?', 1) as ссылка
from marts.price_drops pc
join core.offer o using (source, offer_id)
where pc.source = 'cian'
order by pc.delta_pct
limit 25;
