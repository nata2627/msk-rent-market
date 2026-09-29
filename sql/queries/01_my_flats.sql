\pset border 2
\echo ''
\echo '=== ЛУЧШИЕ ВАРИАНТЫ ПО ОЦЕНКЕ ==='
\echo 'Те же данные и та же оценка, что на вкладке «Подбор» в дашборде.'
\echo 'Своих критериев у этого запроса НЕТ: условия задаются фильтрами'
\echo 'в дашборде, а здесь показан верх общего списка.'
\echo ''
-- Жёстко зашитых критериев здесь больше нет.
--
-- Раньше запрос отбирал по is_my_segment: 1-2 комнаты, от 40 кв.м,
-- животные разрешены, парк в пределах километра. Дашборд этой разметкой
-- не пользовался никогда, и два «моих сегмента» тихо разъехались —
-- однушка 38 кв.м не показывалась здесь ни при каких условиях, а почему,
-- по дашборду понять было невозможно. Одно определение лучше двух.
select score as оценка,
       district as район,
       rooms_count as комн,
       total_area::int as "м2",
       price::int as "цена/мес",
       entry_cost::int as "нужно сразу",
       coalesce(client_fee_pct::int, 0) as "комис,%",
       coalesce(build_year::text, '?') as "дом,год",
       large_park_distance_m as "парк,м",
       metro_real_walk_min as "метро,мин",
       floor || '/' || floors_total as этаж,
       case pets_state when 'allowed' then 'можно'
                       when 'negotiable' then 'по догов.'
                       when 'forbidden' then 'нельзя'
                       else 'не указано' end as животные,
       -- Когда объявление последний раз попадалось в выдаче. Без этого
       -- список молча мешал вчерашние строки с недельными: комнатности
       -- 3+ и студии собираются раз в неделю, и половина ссылок ведёт
       -- на уже снятое.
       case when not is_active
                 then 'снято ' || to_char(disappeared_date, 'DD.MM')
            when last_seen_date = current_date     then 'сегодня'
            when last_seen_date = current_date - 1 then 'вчера'
            else (current_date - last_seen_date) || ' дн. назад'
       end as видели,
       split_part(url, '?', 1) as ссылка
from marts.offer_scored
where source = 'cian'
  and price is not null
  and not coalesce(is_duplicate, false)
  and not coalesce(is_room_share, false)
  and not coalesce(is_daily, false)
  and coalesce(lease_term_type, 'longTerm') = 'longTerm'
order by score desc, price
limit 25;
