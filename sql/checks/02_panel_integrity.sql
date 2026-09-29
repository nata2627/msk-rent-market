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

-- Снятие, датированное днём, в который эту комнатность никто не обходил,
-- утверждает то, чего никто не проверял: объявления не было там, куда не
-- смотрели. Однокомнатные не собирались 23 и 26 сентября 2026, а 774
-- снятия из 2417 стояли именно этими числами — правило брало
-- «последний раз видели + 1», не спрашивая журнал обхода.
--
-- Чинится проходом `make disappeared-backfill`.
\echo 'Снятия, датированные несобранным днём (не должно быть)'
select count(*) as disappeared_on_uncovered_day
from core.offer o
where o.disappeared_date is not null
  and not exists (
      select 1 from raw.collection_run r join raw.bucket b using (run_id)
      where r.source = o.source and r.status = 'ok'
        and r.snapshot_date = o.disappeared_date
        and b.rooms = case when o.rooms_count between 1 and 6
                                then o.rooms_count::text
                           when o.flat_type = 'studio'   then '7'
                           when o.flat_type = 'openPlan' then '9'
                           end);

\echo 'Сколько раз в среднем наблюдалось объявление'
select round(avg(n), 2) as avg_observations, max(n) as max_observations
from (select count(*) as n from core.offer_observation
      group by source, offer_id) t;
