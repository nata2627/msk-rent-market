-- Целостность панели.
\echo 'Наблюдения без карточки объявления (не должно быть ни одного)'
select count(*) as orphan_observations
from core.offer_observation ob
left join core.offer o on o.source = ob.source and o.offer_id = ob.offer_id
where o.offer_id is null;

\echo 'Объявления, увиденные раньше даты первого наблюдения (не должно быть)'
select count(*) as inconsistent_first_seen
from core.offer o
where o.first_seen_date > (
    select min(ob.snapshot_date) from core.offer_observation ob
    where ob.source = o.source and ob.offer_id = o.offer_id
);

\echo 'Активные объявления с проставленной датой исчезновения (не должно быть)'
select count(*) as active_but_disappeared
from core.offer where is_active and disappeared_date is not null;

\echo 'Сколько раз в среднем наблюдалось объявление'
select round(avg(n), 2) as avg_observations, max(n) as max_observations
from (select count(*) as n from core.offer_observation
      group by source, offer_id) t;
