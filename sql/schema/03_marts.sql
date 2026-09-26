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
       o.metro_walk_min, o.park_distance_m, o.large_park_distance_m,
       o.inside_mkad
from core.offer o
join core.offer_observation ob
  on ob.source = o.source and ob.offer_id = o.offer_id
order by ob.source, ob.offer_id, ob.snapshot_date desc;

-- ---------------------------------------------------------------------
-- Разметка сегмента.
--
-- Критерии поиска: 1-2 комнаты, от 40 кв.м, животные разрешены или
-- по договорённости, длительная аренда, не апартаменты, парк рядом.
--
-- Парк вынесен в ОТДЕЛЬНЫЙ флаг, а не вшит в is_my_segment. Причина:
-- park_distance_m заполняется отдельным шагом обогащения по OpenStreetMap
-- и до него равен NULL. Если бы критерий парка входил в основной флаг,
-- до обогащения сегмент был бы пуст, и это выглядело бы как поломка сбора,
-- а не как незаполненное поле.
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
     , (    l.rooms_count in (1, 2)
        and l.total_area >= 40
        and l.pets_state in ('allowed', 'negotiable')
        and coalesce(l.is_apartments, false) = false
        and coalesce(l.is_daily, false) = false
        and coalesce(l.lease_term_type, 'longTerm') = 'longTerm'
       ) as is_my_segment
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
     -- Предоплата из текста (prepay_months) в сумму СОЗНАТЕЛЬНО не
     -- входит, хотя и разобрана. «Первый и последний месяц» — это
     -- обычно и есть залог, названный иначе: у 141 объявления из 222,
     -- где текст говорит про последний месяц, залог в анкете равен
     -- ровно месячной плате. Складывать оба означало бы посчитать одни
     -- и те же деньги дважды — ошибка того же рода, что и прежняя, но
     -- в другую сторону. Поле остаётся в таблице отдельной колонкой:
     -- пусть его видно, но не в итоговой сумме.
     -- Залог больше шести месячных плат в поле не помещается по смыслу:
     -- это опечатка автора. Живой пример — 65 000 000 залога при цене
     -- 65 000, то есть в поле аренды заехала цена продажи. Такие значения
     -- заменяются на месяц, иначе одно объявление уносит верх списка
     -- «дороже всего на входе» и ломает любую шкалу.
     , ( l.price
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
from marts.offer_latest l
left join core.offer_duplicate d
       on d.source = l.source and d.offer_id = l.offer_id;

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
     -- Дешевле похожих — лучше, но только до предела.
     --
     -- Сначала балл рос монотонно: чем дешевле, тем выше. Первое же место
     -- в списке показало, чего это стоит — 60 000 при медиане 85 000 по
     -- району оказались не квартирой, а двумя комнатами в трёхкомнатной,
     -- где третья заперта с вещами хозяина.
     --
     -- Проверка на всей панели подтвердила: глубокая скидка — признак
     -- беды, а не выгоды. У объявлений дешевле похожих на 30% и более
     -- средний год постройки дома 1975 против 2005 у тех, что идут по
     -- рынку, а «требует ремонта», «без ремонта», «бабушкин» встречается
     -- в описании в 18.9% против 4.6%.
     --
     -- Поэтому балл максимален при умеренной скидке около 10% и падает в
     -- обе стороны: и когда дорого, и когда подозрительно дёшево.
     -- Неизвестно (мало сопоставимых) — 50, то есть «не в плюс и не в
     -- минус», а не ноль: отсутствие сравнения не повод штрафовать.
     , coalesce(greatest(0, least(100,
         100 - abs(s.price / nullif(c.med_price, 0) - 1 + 0.10) * 250)), 50)
         as score_price
     -- Возраст дома — замена отсутствующему типу ремонта.
     --
     -- Честно говоря, замена так себе: панель 1988 года может быть с
     -- хорошим ремонтом, а новостройка — с голыми стенами. Но repair_type
     -- в выдаче пуст у всех объявлений до единого, а год постройки есть у
     -- 77%, и связь со «страшным ремонтом» в описаниях по панели видна.
     -- Когда появится обогащение по карточкам, эта составляющая уступит
     -- место настоящему ремонту.
     , case when s.build_year is null   then 50
            when s.build_year >= 2010   then 100
            when s.build_year >= 2000   then 80
            when s.build_year >= 1991   then 60
            when s.build_year >= 1971   then 40
            else 25 end as score_building
     , coalesce(greatest(0, least(100,
         50 + (s.total_area / nullif(c.med_area, 0) - 1) * 250)), 50) as score_area
     , case when s.metro_walk_min is null then 40
            when s.metro_walk_min <= 5  then 100
            when s.metro_walk_min <= 10 then 80
            when s.metro_walk_min <= 15 then 55
            when s.metro_walk_min <= 20 then 30
            else 10 end as score_metro
     , case when s.large_park_distance_m is null  then 25
            when s.large_park_distance_m <= 500   then 100
            when s.large_park_distance_m <= 1000  then 75
            when s.large_park_distance_m <= 2000  then 50
            when s.large_park_distance_m <= 5000  then 25
            else 0 end as score_park
     , case when s.has_furniture is true then 100
            when s.has_furniture is false then 30
            else 50 end as score_furniture
from marts.offer_segment s
left join marts.comparable_median c
       on c.district = s.district and c.rooms_count = s.rooms_count
)
-- Веса. Цена весила 40 и тянула наверх всё дешёвое подряд; теперь 25, и
-- освободившееся отдано возрасту дома — единственному доступному намёку
-- на состояние квартиры.
select scored.*
     , round( 0.25 * score_price
            + 0.20 * score_area
            + 0.15 * score_building
            + 0.15 * score_metro
            + 0.15 * score_park
            + 0.10 * score_furniture )::int as score
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
-- Обновление всех материализованных витрин одной командой.
-- CONCURRENTLY требует уникального индекса (он выше) и не блокирует чтение.
-- ---------------------------------------------------------------------
create or replace function marts.refresh_all() returns void as $$
begin
    refresh materialized view concurrently marts.median_price_by_district;
    refresh materialized view concurrently marts.comparable_median;
    -- Порядок важен: оценка считается из сопоставимых, поэтому она
    -- обновляется после них, а не до.
    refresh materialized view concurrently marts.offer_scored;
exception when others then
    -- CONCURRENTLY падает, если представление ещё ни разу не наполнялось.
    refresh materialized view marts.median_price_by_district;
    refresh materialized view marts.comparable_median;
    refresh materialized view marts.offer_scored;
end;
$$ language plpgsql;
