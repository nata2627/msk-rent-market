-- =====================================================================
-- Слой 3: витрины.
--
-- Здесь и только здесь появляется искомый сегмент. Важно, что он —
-- разметка над полными данными, а не фильтр сбора: рынок собран целиком,
-- а сегмент это столбец, по которому его можно разрезать. Поменяется
-- представление о желаемой квартире — поменяется одно представление,
-- а пересобирать данные не придётся.
-- =====================================================================

create schema if not exists marts;

-- ---------------------------------------------------------------------
-- Последнее наблюдение по каждому объявлению.
-- ---------------------------------------------------------------------
-- Представления пересоздаются, а не заменяются. `create or replace view`
-- не умеет менять состав и порядок колонок, а offer_latest построено через
-- ob.* и обязано подхватывать новые колонки панели при каждом make schema.
-- CASCADE снимает зависящее offer_segment, которое создаётся следом.
drop view if exists marts.offer_latest cascade;

-- Колонки объявления перечислены явно, а не через o.*: у обеих таблиц есть
-- source и offer_id, и звёздочка с двух сторон дала бы две одноимённые
-- колонки — Postgres такое представление не создаст.
create or replace view marts.offer_latest as
select distinct on (ob.source, ob.offer_id)
       ob.*,
       o.url, o.first_seen_date, o.last_seen_date, o.is_active,
       o.disappeared_date, o.published_at, o.rooms_count, o.flat_type,
       o.is_apartments, o.total_area, o.living_area, o.kitchen_area,
       o.floor, o.floors_total, o.building_material, o.build_year, o.has_lift,
       o.address, o.lat, o.lon, o.okrug, o.district, o.street, o.house,
       o.metro_walk_min, o.metro_real_walk_min,
       o.park_distance_m, o.large_park_distance_m,
       o.inside_mkad
from core.offer o
join core.offer_observation ob
  on ob.source = o.source and ob.offer_id = o.offer_id
order by ob.source, ob.offer_id, ob.snapshot_date desc;

-- ---------------------------------------------------------------------
-- Разметка объявлений.
--
-- Зашитых критериев поиска здесь БОЛЬШЕ НЕТ. Раньше был флаг
-- is_my_segment (1-2 комнаты, от 40 кв.м, животные разрешены), но
-- дашборд им не пользовался никогда, и два «моих сегмента» тихо
-- разъехались: в терминале однушка 38 кв.м не показывалась ни при каких
-- условиях, а понять почему, глядя на дашборд, было невозможно.
--
-- Теперь определение одно и живёт в фильтрах дашборда, а здесь — только
-- признаки, из которых оно складывается.
--
-- Парк держится отдельным признаком, а не вшит в условия отбора:
-- park_distance_m заполняется отдельным шагом обогащения по OpenStreetMap
-- и до него равен NULL. Если бы критерий парка входил в общий флаг,
-- до обогащения выборка была бы пуста, и это выглядело бы как поломка
-- сбора, а не как незаполненное поле.
-- ---------------------------------------------------------------------
-- Какой обход был последним для каждой комнатности.
--
-- Свежесть объявления нельзя мерить календарными днями. Однушки и
-- двушки собираются ежедневно, трёшки, студии и свободная планировка —
-- раз в неделю, и «последний раз видели четыре дня назад» означает для
-- них совершенно разное: для двушки это четыре пропущенных обхода, для
-- трёшки — что обхода с тех пор просто не было.
--
-- Поэтому точка отсчёта у каждой комнатности своя. Тот же разрез пара
-- «комнатность и день» уже используется в db.mark_disappeared: там он
-- не даёт записать в снятые то, на что мы не смотрели.
--
-- Считаются только УСПЕШНЫЕ обходы. Упавший прогон не доказывает
-- отсутствия объявления, он доказывает отсутствие прогона.
-- ---------------------------------------------------------------------
create or replace view marts.room_coverage as
with days as (
    select distinct b.rooms as room_code, r.snapshot_date
      from raw.collection_run r
      join raw.bucket b using (run_id)
     where r.status = 'ok' and r.source = 'cian' and b.rooms is not null
), ranked as (
    select room_code, snapshot_date,
           row_number() over (partition by room_code
                                  order by snapshot_date desc) as rn
      from days
)
select room_code,
       max(snapshot_date) filter (where rn = 1) as last_crawl_date,
       max(snapshot_date) filter (where rn = 2) as prev_crawl_date,
       count(*)                                 as crawls_total
  from ranked
 group by room_code;

comment on view marts.room_coverage is
    'Последний и предпоследний успешный обход по каждой комнатности';

-- Все дни успешных обходов, по комнатностям. Нужен, чтобы считать не
-- «сколько дней прошло», а «сколько обходов мы её не застали» — для
-- трёшки это разные числа в семь раз.
create or replace view marts.room_crawl_day as
select distinct b.rooms as room_code, r.snapshot_date
  from raw.collection_run r
  join raw.bucket b using (run_id)
 where r.status = 'ok' and r.source = 'cian' and b.rooms is not null;

-- ---------------------------------------------------------------------
-- Дубль ли это объявление: у группы одинаковых остаётся один
-- представитель, остальные помечаются. Не удаляются: сырые данные и
-- панель не трогаются, а аналитика сама решает, фильтровать или нет.
-- Правдоподобие цены.
--
-- Объявление за 85 000 000 рублей в месяц за 55 квадратных метров — это
-- 1 545 455 рублей за метр при медиане 1 842 и 99.9-м перцентиле 10 671.
-- Почти наверняка в поле аренды указали цену продажи.
--
-- Такие объявления НЕ удаляются: сырые данные и панель не трогаются,
-- а ошибка площадки — сама по себе наблюдение. Но в регрессию их пускать
-- нельзя: на медиану один выброс не влияет, а на МНК влияет сильно.
-- Границы взяты по распределению с запасом, а не назначены круглыми.
create or replace view marts.offer_segment as
select l.*
     , (   l.price < 10000
        or l.price > 5000000
        or (l.total_area > 0 and l.price / l.total_area not between 400 and 15000)
       ) as is_price_outlier
     -- Сдаётся не квартира, а комната или часть квартиры.
     --
     -- Площадка такие объявления не отделяет: flat_type у них 'rooms',
     -- как и у обычных квартир, а площадь указана всей квартиры. В
     -- сравнении с похожими это выглядит как неправдоподобно дешёвая
     -- большая квартира — так одно такое объявление и возглавило список
     -- подбора. Их немного (шесть на 28 тысяч), но каждое всплывает
     -- наверх именно потому, что дёшево.
     , (l.description ~* '(комнат[аыу]?|комнаты)\s+в\s+(двух|трех|трёх|четырех|четырёх|\d)[- ]?комнатн'
        or l.description ~* 'сда[ёе]тся комната|в коммунальн') as is_room_share
     , coalesce(d.canonical_id, l.offer_id) <> l.offer_id as is_duplicate
     , d.matched_on as duplicate_reason
     -- Парк считается по КРУПНОМУ парку, а не по любому: сквером между
     -- домами в OpenStreetMap помечено больше половины зелёных контуров,
     -- и по ним «парк рядом» оказывается у 88% объявлений. По паркам от
     -- 10 га — у 17%, и критерий начинает различать.
     , (l.large_park_distance_m is not null and l.large_park_distance_m <= 1000)
         as is_near_park
     -- Полосы НЕ вложены друг в друга: «до 1 км» не включает «до 500 м».
     -- Это нужно, чтобы фильтр допускал выбор нескольких значений сразу:
     -- при вложенных порогах выбор «до 500 м» и «до 1 км» означал бы
     -- ровно то же, что один «до 1 км», и множественный выбор был бы
     -- бессмысленной тратой кликов.
     , case when l.large_park_distance_m is null      then 'не известно'
            when l.large_park_distance_m <= 500       then 'до 500 м, пешком'
            when l.large_park_distance_m <= 1000      then '500 м – 1 км'
            when l.large_park_distance_m <= 2000      then '1–2 км'
            when l.large_park_distance_m <= 5000      then '2–5 км'
            else 'дальше 5 км' end as park_band
     -- Сколько денег нужно отдать единовременно при заселении.
     --
     -- Именно эта сумма определяет, потянешь ли вариант, а вовсе не
     -- месячная плата: четыре квартиры с одинаковой ценой 60 000 требуют
     -- на входе от 60 000 до 710 000 рублей.
     --
     -- Состав: месяц аренды вперёд + залог + комиссия.
     --
     -- Неуказанный залог считается равным месяцу, а не нулю. Раньше было
     -- ноль, и 1215 объявлений без заполненного поля выглядели на входе
     -- вдвое дешевле, чем стоят на самом деле. Залог в один месяц —
     -- московская норма, нулевой залог — редкость; из двух допущений
     -- второе ошибается чаще и всегда в опасную сторону, в меньшую.
     --
     -- Аренда считается по prepay_months, а не всегда за один месяц.
     --
     -- Сначала предоплата в сумму не входила: она бралась из свободного
     -- описания, где «первый и последний месяц» — чаще всего тот же
     -- залог, названный иначе, и сложение посчитало бы одни деньги
     -- дважды. Потом выяснилось, что площадка собирает строку условий
     -- сама: «Залог 58 000 ₽, комиссия 50%, предоплата 1 месяц, на
     -- длительный срок». Она заполнена у всех 112 289 снимков, и залог
     -- с предоплатой перечислены в ней как РАЗНЫЕ условия — значит
     -- складывать можно. Предоплата в два месяца и больше стоит у 1738
     -- наблюдений; у 893 из них залог при этом равен месячной плате,
     -- то есть на входе нужно три месяца, а не два.
     --
     -- Залог больше шести месячных плат в поле не помещается по смыслу:
     -- это опечатка автора. Живой пример — 65 000 000 залога при цене
     -- 65 000, то есть в поле аренды заехала цена продажи. Такие значения
     -- заменяются на месяц, иначе одно объявление уносит верх списка
     -- «дороже всего на входе» и ломает любую шкалу.
     , ( l.price * coalesce(l.prepay_months, 1)
       + case when l.deposit_rub is not null and l.deposit_rub <= l.price * 6
                   then l.deposit_rub
              when l.deposit_months is not null
                   then l.price * l.deposit_months
              else l.price end
       + case when coalesce(l.client_fee_pct, 0) > 0
              then l.price * l.client_fee_pct / 100.0
              else coalesce(l.commission_rub, 0) end
       )::numeric(12,0) as entry_cost
     -- Залог не указан (или указан неправдоподобно) и взят по умолчанию.
     -- Тогда entry_cost — оценка, а не факт, и выдавать её за точную
     -- сумму нельзя.
     , (   (l.deposit_rub is null and l.deposit_months is null)
        or (l.deposit_rub is not null and l.deposit_rub > l.price * 6)
       ) as deposit_assumed
     -- Свежесть в обходах, а не в днях. См. marts.room_coverage.
     --
     -- Застали ли мы объявление в последнем обходе ЕГО комнатности.
     -- Это не то же самое, что «снято»: снятие подтверждается только
     -- после трёх собранных дней отсутствия, и до тех пор объявление
     -- считается активным. Но для того, кто собирается звонить, разница
     -- между «было в последней выдаче» и «в последней выдаче его уже
     -- нет» — главная, и она должна быть видна сразу, а не через три дня.
     -- Счётчик, а не флаг «пропущено два обхода». Флаг сваливал в одну
     -- кучу объявление, пропавшее вчера, и объявление из веб-архива,
     -- которое в живой выдаче не встречалось ни разу: у архивных
     -- last_seen_date — декабрь 2025, и «пропущено два обхода» про них
     -- формально верно и совершенно бесполезно. Число различает.
     , coalesce(m.crawls_missed, 0)              as crawls_missed
     , (coalesce(m.crawls_missed, 0) = 0)        as seen_in_last_crawl
     , rc.last_crawl_date
from marts.offer_latest l
left join core.offer_duplicate d
       on d.source = l.source and d.offer_id = l.offer_id
-- Код комнатности площадки, а не rooms_count: студии и свободная
-- планировка живут в flat_type, и обходятся они своими корзинами.
-- Комнатность вперёд вида — так же, как в db.mark_disappeared.
left join marts.room_coverage rc
       on rc.room_code = case when l.rooms_count between 1 and 6
                                   then l.rooms_count::text
                              when l.flat_type = 'studio'   then '7'
                              when l.flat_type = 'openPlan' then '9'
                         end
left join lateral (
    select count(*) as crawls_missed
      from marts.room_crawl_day cd
     where cd.room_code = case when l.rooms_count between 1 and 6
                                    then l.rooms_count::text
                               when l.flat_type = 'studio'   then '7'
                               when l.flat_type = 'openPlan' then '9'
                          end
       and cd.snapshot_date > l.last_seen_date
) m on true;

-- ---------------------------------------------------------------------
-- Уровень района: насколько он дороже или дешевле города.
--
-- Нужен потому, что цена и площадь в оценке меряются ВНУТРИ района, и
-- без этой поправки оценка не отличала хороший район от плохого:
-- дешёвая квартира в Ново-Переделкино получала ровно столько же очков,
-- сколько дешёвая в Хамовниках. Связь итоговой оценки с уровнем цен
-- района была −0.175 — то есть слабо отрицательной, оценка ещё и
-- подтягивала наверх дешёвые окраины.
--
-- Меряется медианой цены за метр, а не общей ценой: иначе районы с
-- большими квартирами выглядели бы дороже при той же ставке за метр.
-- Разброс по 138 районам от 0.58 (Новофёдоровское) до 1.93
-- (Пресненский), четверти на 0.89 и 1.21.
--
-- Оговорка, которую надо держать в голове: это ПРЕСТИЖ, а не
-- «спокойствие». Хамовники дорогие и тихие, Тверская дорогая и шумная,
-- и по цене их не различить. Для тишины нужен отдельный признак —
-- расстояние до крупных дорог и промзон по OpenStreetMap.
-- ---------------------------------------------------------------------
drop materialized view if exists marts.district_level cascade;
create materialized view marts.district_level as
with d as (
  select district
       , count(*)                                                    as offers
       , percentile_cont(0.5) within group (order by price / total_area) as ppm
  from marts.offer_segment
  where source = 'cian' and price is not null and total_area > 0
    and district is not null and rooms_count in (1, 2)
    and not coalesce(is_duplicate, false)
    and not coalesce(is_price_outlier, false)
  group by 1 having count(*) >= 10),
city as (select percentile_cont(0.5) within group (order by ppm) as ppm from d)
select d.district, d.offers, d.ppm::numeric(12,0) as district_ppm,
       (d.ppm / city.ppm)::numeric(6,3) as ppm_vs_city
from d, city;

create unique index if not exists district_level_key
    on marts.district_level (district);

-- ---------------------------------------------------------------------
-- Сопоставимые квартиры: медиана по району и комнатности.
--
-- База для оценки «дешевле или дороже похожих». Считается по ВСЕМУ
-- рынку, а не по отфильтрованной выборке: иначе «ниже среднего» означало
-- бы «ниже среднего среди того, что я уже отобрала», и сужение фильтров
-- само по себе меняло бы оценку каждой квартиры.
--
-- Порог в пять объявлений на ячейку — чтобы медиана вообще что-то
-- значила. Где вариантов меньше, сравнивать не с чем, и оценка по цене
-- честно остаётся неизвестной.
-- ---------------------------------------------------------------------
-- Материализованное, а не обычное: offer_segment считается из панели в
-- 89 тысяч наблюдений через distinct on, и обычное представление
-- заставляло планировщик проходить её дважды — один раз ради самих
-- объявлений, второй ради медиан. Верхние восемь строк подбора
-- выбирались три с половиной минуты. Обновляется в marts.refresh_all().
drop materialized view if exists marts.comparable_median cascade;
drop view if exists marts.comparable_median cascade;
create materialized view marts.comparable_median as
select district
     , rooms_count
     , count(*)                                                          as comparable_offers
     , percentile_cont(0.5) within group (order by price)                as med_price
     , percentile_cont(0.5) within group (order by total_area)           as med_area
from marts.offer_segment
where source = 'cian'
  and price is not null and total_area > 0
  and district is not null and rooms_count is not null
  and not coalesce(is_duplicate, false)
  and not coalesce(is_price_outlier, false)
  and not coalesce(is_daily, false)
group by 1, 2
having count(*) >= 5;

-- Уникальный индекс нужен для REFRESH ... CONCURRENTLY: без него
-- обновление блокирует чтение, и дашборд на время пересчёта пустеет.
create unique index if not exists comparable_median_key
    on marts.comparable_median (district, rooms_count);

-- ---------------------------------------------------------------------
-- Оценка варианта: одно число вместо чтения тридцати объявлений подряд.
--
-- Это НЕ модель и не предсказание — это взвешенная сумма предпочтений,
-- записанная явно, чтобы её можно было оспорить и поменять. Никакой
-- оценки «хорошая квартира» здесь нет и быть не может: есть оценка
-- «похоже на то, что я ищу».
--
-- Каждая составляющая приведена к шкале 0-100, итог — взвешенная сумма.
-- Веса стоят рядом с формулой, а не в коде дашборда: считаться должно
-- один раз и одинаково везде.
--
-- Цена и площадь меряются ОТНОСИТЕЛЬНО похожих квартир, а не в рублях и
-- метрах. Сорок метров в Капотне и в Хамовниках — это разные сорок
-- метров, и абсолютная шкала сравнивала бы районы, а не квартиры.
-- Шаг шкалы: отклонение на 20% от медианы даёт крайние 0 или 100.
--
-- Чего в оценке нет и почему: РЕМОНТА. Поле repair_type в выдаче поиска
-- пусто у всех 28 020 объявлений до единого — площадка его отдаёт только
-- на странице самого объявления. Добавлять вес за то, чего в данных нет,
-- значило бы выдумывать; когда появится обогащение по карточкам,
-- составляющая встанет сюда.
-- ---------------------------------------------------------------------
-- Материализованное. Витрина строится из панели в 89 тысяч наблюдений
-- через distinct on, поверх неё считается регулярное выражение по
-- описаниям и присоединяются медианы сопоставимых. Обычным
-- представлением каждая карточка дашборда платила за весь этот путь
-- заново: гистограмма под фильтрами рисовалась 11 секунд.
--
-- Данные меняются раз в сутки, поэтому ничего не теряется. Обновляется
-- в marts.refresh_all(), то есть шагом «Витрины» ежедневного прогона.
drop materialized view if exists marts.offer_scored cascade;
drop view if exists marts.offer_scored cascade;
create materialized view marts.offer_scored as
with scored as (
select s.*
     , c.comparable_offers
     , c.med_price
     , c.med_area
     , dl.ppm_vs_city
     -- ЦЕНА КАК СИГНАЛ КАЧЕСТВА, а не как выгода. Знак перевёрнут.
     --
     -- Сначала балл рос, когда квартира дешевле похожих, — и наверх
     -- лезло всё дешёвое и убитое. Измерено на панели: связь отклонения
     -- цены от медианы района с годом постройки дома +0.30, с упоминанием
     -- «требует ремонта» в описании −0.13. То есть цена — это оценка
     -- качества, которую рынок уже сделал за нас, и дешевизна означает
     -- не удачу, а то, чего мы не видим: состояние, вид, шум, этаж.
     --
     -- Цена за метр проверена как замена и оказалась не лучше: связь с
     -- годом дома 0.276 против 0.300, с плохим ремонтом −0.143 против
     -- −0.134. Разница в третьем знаке, поэтому оставлена цена целиком —
     -- площадь уже учтена отдельной составляющей.
     --
     -- Насыщение на +20%: выше это уже не качество, а оптимизм
     -- арендодателя. Бюджет задаётся фильтром «цена до», а не здесь.
     --
     -- ОГОВОРКА: составляющая отчасти тавтологична — «дорогое значит
     -- хорошее». Это заплатка на время, пока нет типа ремонта; он есть
     -- только на странице объявления.
     , coalesce(greatest(0, least(100,
         50 + (s.price / nullif(c.med_price, 0) - 1) * 250)), 50) as score_price
     , coalesce(greatest(0, least(100,
         50 + (s.total_area / nullif(c.med_area, 0) - 1) * 250)), 50) as score_area
     -- УРОВЕНЬ РАЙОНА. Без него оценка сравнивала квартиру только с
     -- соседями по двору и не отличала Хамовники от Ново-Переделкино.
     , coalesce(greatest(0, least(100,
         50 + (dl.ppm_vs_city - 1) * 120)), 50) as score_district
     -- ДОМ: эпоха и материал вместе, а не один год постройки.
     --
     -- Прежняя шкала «новее — лучше» противоречит вкусу владелицы
     -- (старый фонд) и данным: рынок платит за метр в домах до 1918 года
     -- 2444 рубля, в сталинках 2120, а в панели 1961-1990 — 1579.
     -- Материал добавляет то, чего год не знает: кирпич 1967 года и
     -- панель 1970-го — разные дома, и разница в цене за метр 350 рублей.
     --
     -- Самая дешёвая эпоха вовсе не панель, а 1991-2009 (1451 за метр) —
     -- поздняя панель и блок без обаяния старого фонда и без качества
     -- новостроек.
     , case when s.building_material in ('old', 'stalin')            then 100
            when s.build_year is not null and s.build_year < 1918    then 100
            when s.build_year between 1918 and 1960                  then  90
            when s.building_material = 'monolithBrick'               then  85
            when s.building_material = 'monolith'                    then  78
            when s.building_material = 'brick'                       then  75
            when s.building_material in ('panel', 'block')           then  30
            when s.build_year >= 2010                                then  75
            when s.build_year between 1991 and 2009                  then  45
            when s.build_year between 1961 and 1990                  then  35
            else 50 end as score_building
     -- МЕТРО — только метрополитен, без МЦК и диаметров.
     --
     -- Диаметр ходит по расписанию с интервалом в 10-15 минут, метро —
     -- каждые две, и «семь минут до станции» означает совершенно разное
     -- время в пути. Из 8958 объявлений с пешей доступностью до 10 минут
     -- 889 держались на диаметре, а у 914 пешего метро нет вовсе.
     , case when s.metro_real_walk_min is null      then  15
            when s.metro_real_walk_min <= 10        then 100
            when s.metro_real_walk_min <= 15        then  90
            when s.metro_real_walk_min <= 20        then  80
            when s.metro_real_walk_min <= 25        then  55
            when s.metro_real_walk_min <= 30        then  35
            else 20 end as score_metro
     , case when s.large_park_distance_m is null  then 25
            when s.large_park_distance_m <= 500   then 100
            when s.large_park_distance_m <= 1000  then 75
            when s.large_park_distance_m <= 2000  then 50
            when s.large_park_distance_m <= 5000  then 25
            else 0 end as score_park
     -- ЖИВОТНЫЕ. Явное разрешение и «по договорённости» почти равны:
     -- разница в пять очков, как и просила владелица. «Не указано» —
     -- середина, это 57% рынка и повод спросить, а не повод вычеркнуть.
     , case when s.pets_state = 'allowed'    then 100
            when s.pets_state = 'negotiable' then  95
            when s.pets_state = 'forbidden'  then   0
            else 50 end as score_pets
     -- Апартаменты — минимум очков, но НЕ исключение из подбора.
     -- Жилыми помещениями они не считаются, коммунальные платежи выше.
     , case when s.is_apartments is true then 0
            when s.is_apartments is false then 100
            else 50 end as score_apartments
     -- ЭТАЖ. Рынок оценивает это сам: первый этаж стоит 1571 рубль за
     -- метр против 1786 на средних, последний — 1700.
     , case when s.floor is null or s.floors_total is null then 70
            when s.floor = 1                               then 40
            when s.floor = s.floors_total                  then 70
            else 100 end as score_floor
from marts.offer_segment s
left join marts.comparable_median c
       on c.district = s.district and c.rooms_count = s.rooms_count
left join marts.district_level dl
       on dl.district = s.district
)
-- Веса. Сумма ровно 100, чтобы итог читался как проценты.
--
-- Оценка за мебель убрана: смысла в ней владелица не увидела, а 97%
-- объявлений имели одно и то же значение, так что она почти ничего не
-- различала.
--
-- Веса спорны по построению, и спорить с ними нужно: опись полей с
-- пустой колонкой под свои веса собирается командой
-- `python -m etl.field_report > docs/fields.md`.
select scored.*
     , round( 0.18 * score_building
            + 0.15 * score_metro
            + 0.15 * score_district
            + 0.14 * score_price
            + 0.12 * score_area
            + 0.10 * score_park
            + 0.08 * score_pets
            + 0.05 * score_apartments
            + 0.03 * score_floor )::int as score
from scored;

-- Для REFRESH ... CONCURRENTLY: без уникального индекса обновление
-- блокирует чтение, и дашборд на время пересчёта пустеет.
create unique index if not exists offer_scored_key
    on marts.offer_scored (source, offer_id);
create index if not exists offer_scored_score_idx
    on marts.offer_scored (score desc);

-- ---------------------------------------------------------------------
-- Медианы по району и комнатности в динамике.
--
-- Медиана, а не среднее: распределение арендных ставок скошено вправо,
-- несколько объявлений за миллион рублей утащат среднее туда, где не живёт
-- ни одна реальная квартира.
--
-- Материализованное представление: запрос тяжёлый, а данные меняются раз
-- в сутки. Обновляется командой `make marts`.
-- ---------------------------------------------------------------------
create materialized view if not exists marts.median_price_by_district as
select ob.snapshot_date
     , o.okrug
     , o.district
     , o.rooms_count
     , count(*)                                                        as offers
     , percentile_cont(0.25) within group (order by ob.price)::numeric(12,0) as p25
     , percentile_cont(0.50) within group (order by ob.price)::numeric(12,0) as median_price
     , percentile_cont(0.75) within group (order by ob.price)::numeric(12,0) as p75
     , percentile_cont(0.50) within group (order by ob.price / nullif(o.total_area, 0))::numeric(12,0)
                                                                       as median_price_per_sqm
from core.offer_observation ob
join core.offer o on o.source = ob.source and o.offer_id = ob.offer_id
where ob.price is not null
  and not ob.is_daily
  and o.total_area is not null
group by ob.snapshot_date, o.okrug, o.district, o.rooms_count;

create unique index if not exists median_price_by_district_key
    on marts.median_price_by_district(snapshot_date, okrug, district, rooms_count);

-- ---------------------------------------------------------------------
-- Подешевевшие объявления: прямая практическая польза к ноябрю.
-- ---------------------------------------------------------------------
create or replace view marts.price_drops as
select pc.*
     , o.district
     , o.rooms_count
     , o.total_area
     , o.url
     , o.first_seen_date
     , pc.changed_on - o.first_seen_date as days_on_market_at_drop
from core.price_change pc
join core.offer o on o.source = pc.source and o.offer_id = pc.offer_id
where pc.direction = 'down';

-- ---------------------------------------------------------------------
-- Ушедшие с рынка: цена того, что перестало висеть, против цены того,
-- что висит до сих пор.
--
-- Зачем отдельная витрина. Медиана выдачи и даже индекс по совпавшим
-- объявлениям описывают СТОК: то, что лежит на полке. А вопрос «сколько
-- на самом деле стоит квартира» — про то, что с полки уходит. Сток
-- засорён объявлениями, которые висят именно потому, что по своей цене
-- никому не нужны, и чем дольше наблюдение, тем больше их доля.
--
-- ПЕРВАЯ И ГЛАВНАЯ ОГОВОРКА: исчезновение объявления это НЕ сдача
-- квартиры. Объявление снимают, когда квартиру сдали, когда передумали
-- сдавать, когда переложили к другому агенту и когда у публикации вышел
-- срок. Различить причины по выдаче нельзя. И цена здесь по-прежнему
-- ЗАПРАШИВАЕМАЯ — последняя, которую автор просил перед тем, как убрать
-- объявление. Цен сделок у нас нет и не будет.
--
-- Когорта — только настоящие новые публикации, появившиеся не раньше
-- первого дня панели. Иначе левое усечение: объявление, которое мы
-- застали уже висящим, прожило неизвестно сколько до нашей первой
-- встречи, и «ушло за один обход» у него значило бы совсем другое.
-- Возраст считается от даты публикации площадки, а не от первой встречи:
-- проверено в src/survival.py, у 38 990 объявлений нет ни одного, где
-- первая встреча раньше публикации, то есть площадка дату не поднимает.
--
-- Скорость ухода мерится В ОБХОДАХ, а не в днях: это то, что мы
-- действительно наблюдали. Объявление, увиденное в одном обходе и
-- больше ни в одном, пробыло на полке меньше двух суток.
--
-- Цена сравнивается с медианой своего района и комнатности
-- (marts.comparable_median), а не в рублях: сорок метров в Капотне и в
-- Хамовниках это разные сорок метров, и абсолютная шкала сравнивала бы
-- районы, а не скорость ухода. Отношение берётся в логарифме — ставки
-- логнормальны, и среднее логарифмов даёт геометрическое среднее, по
-- которому проценты складываются, а не расползаются.
--
-- Неудобные наблюдения не выброшены, а помечены: exclude_reason говорит,
-- почему строка не идёт в счёт, и карточка «Что сюда не вошло»
-- показывает, сколько их. Порог отношения 0,2-5 отсекает опечатки
-- площадки (85 млн руб./мес за 55 м² — настоящая строка в raw).
-- ---------------------------------------------------------------------
drop materialized view if exists marts.offer_exit cascade;
create materialized view marts.offer_exit as
with panel_start as (
    select min(snapshot_date) as d0
    from core.offer_observation where source = 'cian'),
-- Последний ЗАВЕРШЁННЫЙ обход, а не последняя дата в панели: сегодняшний
-- день уже есть в наблюдениях, но собран наполовину.
last_ok as (
    select max(snapshot_date) as d1
    from raw.collection_run where source = 'cian' and status = 'ok'),
seen as (
    select offer_id, count(distinct snapshot_date) as crawls
    from core.offer_observation where source = 'cian' group by 1)
select s.source
     , s.offer_id
     , s.district
     , s.rooms_count
     , s.total_area
     , s.price                                            as price
     , s.med_price
     , s.published_at::date                               as published_on
     , s.first_seen_date
     , s.last_seen_date
     , s.disappeared_date
     , sn.crawls
     , s.disappeared_date is not null                     as has_left
     , s.disappeared_date - s.published_at::date          as age_at_exit
     , ln(s.price / s.med_price)                          as log_rel_price
     -- Было ли у публикации время не только исчезнуть, но и подтвердить
     -- исчезновение. Без этой отметки последний день ряда всегда провален:
     -- 03.10.2026 показывал 1,5% быстрых уходов против 7-10% в остальные
     -- дни — не потому, что рынок встал, а потому, что подтвердиться они
     -- не успели.
     --
     -- Четыре дня, и каждый за своё. Объявление, опубликованное после
     -- десяти утра, попадает в выдачу только следующим обходом — это
     -- один день. Снятие подтверждается тремя СОБРАННЫМИ днями отсутствия
     -- (DISAPPEARED_AFTER_DAYS = 3) — это ещё три. Порог посчитан от
     -- константы сбора, а не назначен на глаз.
     , s.published_at::date <= l.d1 - 4                   as exit_observable
     -- Границы групп взяты для читаемости таблицы, а не выведены из
     -- данных. Вывод от них не зависит: рядом на графике тот же разрез по
     -- каждому обходу отдельно, без всяких границ.
     , case when s.disappeared_date is null then 5
            when sn.crawls = 1 then 1
            when sn.crawls <= 3 then 2
            when sn.crawls <= 7 then 3
            else 4 end                                    as speed_order
     , case when s.disappeared_date is null then 'ещё висит'
            when sn.crawls = 1  then 'ушло за один обход'
            when sn.crawls <= 3 then 'ушло за 2-3 обхода'
            when sn.crawls <= 7 then 'ушло за 4-7 обходов'
            else 'ушло за 8 и больше обходов' end          as speed_group
     , case when coalesce(s.is_duplicate, false)   then 'дубль'
            when coalesce(s.is_room_share, false)  then 'комната, а не квартира'
            when coalesce(s.is_price_outlier, false) then 'выброс по цене'
            when s.price / s.med_price < 0.2 or s.price / s.med_price > 5
                 then 'отношение к сопоставимым вне 0,2-5'
            end                                           as exclude_reason
from marts.offer_scored s
cross join panel_start p
cross join last_ok l
join seen sn on sn.offer_id = s.offer_id
where s.source = 'cian'
  and s.rooms_count in (1, 2)
  and s.published_at is not null
  and s.published_at::date >= p.d0
  and s.first_seen_date >= s.published_at::date
  and s.price > 0
  and s.total_area > 0
  and s.med_price > 0;

create unique index if not exists offer_exit_key
    on marts.offer_exit (source, offer_id);

-- ---------------------------------------------------------------------
-- Обновление всех материализованных витрин одной командой.
-- CONCURRENTLY требует уникального индекса (он выше) и не блокирует чтение.
-- ---------------------------------------------------------------------
create or replace function marts.refresh_all() returns void as $$
begin
    refresh materialized view concurrently marts.median_price_by_district;
    refresh materialized view concurrently marts.comparable_median;
    refresh materialized view concurrently marts.district_level;
    -- Порядок важен: оценка считается из сопоставимых, поэтому она
    -- обновляется после них, а не до.
    refresh materialized view concurrently marts.offer_scored;
    -- Ушедшие считаются из оценки (там уже сведены сопоставимые и метки
    -- дублей), поэтому строго после неё.
    refresh materialized view concurrently marts.offer_exit;
exception when others then
    -- CONCURRENTLY падает, если представление ещё ни разу не наполнялось.
    refresh materialized view marts.median_price_by_district;
    refresh materialized view marts.comparable_median;
    refresh materialized view marts.district_level;
    refresh materialized view marts.offer_scored;
    refresh materialized view marts.offer_exit;
end;
$$ language plpgsql;
