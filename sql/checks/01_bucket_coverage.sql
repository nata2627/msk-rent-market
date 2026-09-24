-- Полнота обхода: по каким корзинам собрано меньше, чем заявил ЦИАН.
-- Расхождение — сигнал, что корзина недобрана: уехала пагинация, оборвалась
-- сеть или корзина не влезла в предел выдачи и не разделилась.
\echo 'Недобор по корзинам последнего запуска'
select b.bucket_id, b.district, b.rooms, b.price_min, b.price_max,
       b.claimed_count, b.collected_count, b.missing, b.error
from raw.bucket_underfilled b
where b.run_id = (select max(run_id) from raw.collection_run)
order by b.missing desc
limit 30;

\echo 'Усечённые корзины: больше предела выдачи и делить их нечем'
select district, rooms, price_min, price_max, claimed_count, collected_count
from raw.bucket
where is_truncated
  and run_id = (select max(run_id) from raw.collection_run);

\echo 'Сводка по запускам'
select run_id, mode, snapshot_date, status, buckets_total, buckets_underfilled,
       offers_seen, requests_made,
       finished_at - started_at as duration
from raw.collection_run
order by run_id desc
limit 10;
