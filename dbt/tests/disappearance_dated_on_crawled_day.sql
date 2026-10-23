-- Снятие, датированное днём, в который эту комнатность не обходили,
-- утверждает то, чего никто не проверял. 26.09.2026 так стояли 774 снятия
-- из 2417. Перенесено из sql/checks/02_panel_integrity.sql.
select o.source, o.offer_id, o.disappeared_date
from {{ source('core', 'offer') }} o
where o.disappeared_date is not null
  and not exists (
      select 1 from {{ source('raw', 'collection_run') }} r
      join {{ source('raw', 'bucket') }} b using (run_id)
      where r.source = o.source and r.status = 'ok'
        and r.snapshot_date = o.disappeared_date
        and b.rooms = case when o.rooms_count between 1 and 6
                                then o.rooms_count::text
                           when o.flat_type = 'studio'   then '7'
                           when o.flat_type = 'openPlan' then '9'
                           end)
