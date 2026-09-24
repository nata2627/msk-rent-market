-- Заполненность полей. Пустая колонка — это чаще всего ошибка разбора,
-- а не свойство рынка, и заметить её надо до, а не после регрессии.
\echo 'Доля заполненных полей в последнем снимке'
select count(*) as offers,
       round(100.0 * count(o.lat)             / count(*), 1) as pct_coords,
       round(100.0 * count(o.district)        / count(*), 1) as pct_district,
       round(100.0 * count(o.total_area)      / count(*), 1) as pct_area,
       round(100.0 * count(o.floor)           / count(*), 1) as pct_floor,
       round(100.0 * count(o.metro_walk_min)  / count(*), 1) as pct_metro_walk,
       round(100.0 * count(ob.price)          / count(*), 1) as pct_price,
       round(100.0 * count(ob.deposit_rub)    / count(*), 1) as pct_deposit,
       round(100.0 * count(ob.description)    / count(*), 1) as pct_description
from core.offer_observation ob
join core.offer o on o.source = ob.source and o.offer_id = ob.offer_id
where ob.snapshot_date = (select max(snapshot_date) from core.offer_observation);

\echo 'Четыре состояния «животных»: как распределились'
select pets_state, count(*),
       round(100.0 * count(*) / sum(count(*)) over (), 1) as pct
from core.offer_observation
where snapshot_date = (select max(snapshot_date) from core.offer_observation)
group by pets_state order by count(*) desc;

\echo 'Новые поля площадки, которых нет в разборе'
select key, count(*) as offers
from core.offer_observation ob,
     lateral jsonb_array_elements_text(ob.unmapped_keys) as key
where ob.snapshot_date = (select max(snapshot_date) from core.offer_observation)
group by key order by offers desc limit 25;
