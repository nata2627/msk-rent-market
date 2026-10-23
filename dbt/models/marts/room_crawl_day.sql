{{ config(materialized='view') }}

-- Все дни успешных обходов, по комнатностям. Нужен, чтобы считать не
-- «сколько дней прошло», а «сколько обходов мы её не застали» — для
-- трёшки это разные числа в семь раз.
select distinct b.rooms as room_code, r.snapshot_date
  from {{ source('raw', 'collection_run') }} r
  join {{ source('raw', 'bucket') }} b using (run_id)
 where r.status = 'ok' and r.source = 'cian' and b.rooms is not null
