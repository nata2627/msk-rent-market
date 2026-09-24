-- =====================================================================
-- Слой 2: панель.
--
-- Ядро проекта. Одно объявление наблюдается много раз, и именно это
-- отличает панель от разовой выгрузки: видно цену на каждую дату,
-- изменение условий и дату исчезновения из выдачи.
--
-- Разделение на две таблицы не косметическое:
--   core.offer             — то, что у квартиры не меняется (площадь, этаж,
--                            адрес, координаты) плюс даты жизни объявления;
--   core.offer_observation — то, что меняется (цена, депозит, комиссия,
--                            мебель, текст), по строке на каждую дату снимка.
-- Если бы всё лежало в одной таблице, площадь дублировалась бы в каждом
-- наблюдении, а изменение цены было бы неотличимо от исправления опечатки
-- в площади.
-- =====================================================================

create schema if not exists core;

-- ---------------------------------------------------------------------
-- Объявление: устойчивые характеристики и жизненный цикл.
-- ---------------------------------------------------------------------
create table if not exists core.offer (
    source             text not null,
    offer_id           text not null,
    primary key (source, offer_id),

    url                text,

    -- Жизненный цикл в НАШИХ наблюдениях, а не на площадке.
    first_seen_date    date not null,
    last_seen_date     date not null,
    is_active          boolean not null default true,
    -- Дата первого полного обхода, в котором объявления уже не было.
    -- ВНИМАНИЕ: это НЕ дата сдачи квартиры. Объявление могли снять по любой
    -- причине: хозяин передумал, риелтор сменил площадку, аккаунт забанили.
    -- Эту оговорку нести через весь анализ, не подменять понятия.
    disappeared_date   date,
    published_at       timestamptz,   -- дата публикации по данным площадки

    -- Квартира.
    rooms_count        smallint,      -- NULL у студий и свободной планировки
    flat_type          text,          -- 'rooms' | 'studio' | 'openPlan'
    -- Апартаменты — юридически не жильё: нельзя прописаться, другие тарифы
    -- ЖКУ. Отдельный флаг, а не разновидность квартиры.
    is_apartments      boolean,
    total_area         numeric(8,2),
    living_area        numeric(8,2),
    kitchen_area       numeric(8,2),

    -- Дом.
    floor              smallint,
    floors_total       smallint,
    building_material  text,
    build_year         smallint,
    has_lift           boolean,

    -- География. Координаты обязательны с первого дня: без них не посчитать
    -- расстояние до парка, а добрать их задним числом по адресу — потери.
    address            text,
    lat                double precision,
    lon                double precision,
    okrug              text,
    district           text,
    street             text,
    house              text,
    -- Минимальное время пешком до метро среди всех станций объявления.
    -- Полный список станций — в core.offer_metro.
    metro_walk_min     smallint,
    -- Расстояние до ближайшего парка по данным OpenStreetMap.
    -- Заполняется отдельным шагом обогащения, не при сборе.
    park_distance_m    integer,

    updated_at         timestamptz not null default now()
);

create index if not exists offer_active_idx   on core.offer(is_active);
create index if not exists offer_district_idx on core.offer(district);
create index if not exists offer_geo_idx      on core.offer(lat, lon);

-- Станции метро объявления: их обычно несколько, поэтому отдельная таблица,
-- а не три колонки «метро 1 / метро 2 / метро 3».
create table if not exists core.offer_metro (
    source         text not null,
    offer_id       text not null,
    station_name   text not null,
    primary key (source, offer_id, station_name),
    time_min       smallint,
    transport_type text,          -- 'walk' | 'transport'
    line_name      text,
    foreign key (source, offer_id) references core.offer(source, offer_id) on delete cascade
);

-- ---------------------------------------------------------------------
-- Наблюдение панели: объявление на дату снимка.
--
-- Ключ (source, offer_id, snapshot_date) и даёт идемпотентность: повторный
-- запуск в тот же день не плодит дубли, а переписывает наблюдение свежим
-- чтением того же дня.
-- ---------------------------------------------------------------------
create table if not exists core.offer_observation (
    source                 text not null,
    offer_id               text not null,
    snapshot_date          date not null,
    primary key (source, offer_id, snapshot_date),
    run_id                 bigint references raw.collection_run(run_id),
    observed_at            timestamptz not null default now(),

    -- Деньги. Цена — объясняемая переменная всего проекта, поэтому вокруг
    -- неё собирается всё, что влияет на реальную стоимость месяца аренды.
    price                  numeric(12,2),
    currency               text,
    payment_period         text,          -- 'month' | 'day'
    utilities_included     boolean,       -- ЖКУ входят в цену
    utilities_price        numeric(12,2), -- если не входят и сумма указана
    meters_not_included    boolean,       -- счётчики оплачиваются отдельно
    deposit_rub            numeric(12,2),
    -- Депозит в карточке бывает задан суммой, а в тексте — числом месяцев.
    -- Храним оба вида: приводить месяцы к рублям на этапе загрузки значило бы
    -- потерять то, как условие сформулировано на самом деле.
    deposit_months         numeric(4,2),
    client_fee_pct         numeric(6,2),  -- комиссия, которую платит арендатор
    agent_fee_pct          numeric(6,2),
    commission_rub         numeric(12,2),
    prepay_months          smallint,
    lease_term_type        text,          -- 'longTerm' | 'fewMonths' | 'daily'
    -- Посуточные из аналитики исключаются, но собираются и лежат с пометкой.
    is_daily               boolean not null default false,

    -- Условия проживания.
    -- Четыре состояния, а не булев флаг: «не указано» и «запрещено» — разные
    -- вещи, и схлопывание их в false испортит оценку надбавки за животных.
    -- 'negotiable' в структурированных полях ЦИАНа отсутствует и достаётся
    -- из текста описания (см. etl/parse.py).
    pets_state             text check (pets_state in
                               ('allowed', 'forbidden', 'negotiable', 'unknown')),
    pets_raw               boolean,       -- как отдала площадка, до разбора текста
    children_allowed       boolean,

    -- Состояние квартиры.
    repair_type            text,
    has_furniture          boolean,
    has_kitchen_furniture  boolean,
    has_washer             boolean,
    has_fridge             boolean,
    has_dishwasher         boolean,
    has_conditioner        boolean,
    has_tv                 boolean,
    has_internet           boolean,
    has_bathtub            boolean,
    has_shower             boolean,
    balconies_count        smallint,
    loggias_count          smallint,
    separate_wc_count      smallint,
    combined_wc_count      smallint,

    -- Текст. Хранится целиком: он нужен и для дедупликации, и для извлечения
    -- условий, которых нет в структурированных полях.
    description            text,
    description_sha1       text,

    -- Автор.
    seller_type            text,          -- 'homeowner' | 'agency' | 'realtor' | ...
    seller_name            text,
    seller_id              text,
    seller_offers_count    integer,
    is_by_homeowner        boolean,

    photos_count           smallint,
    edited_at              timestamptz,   -- дата обновления на площадке

    -- Ключи payload, которые разбор не распознал. Не мусор, а диагностика:
    -- площадка меняет схему молча, и это единственный способ заметить.
    unmapped_keys          jsonb
);

create index if not exists observation_date_idx  on core.offer_observation(snapshot_date);
create index if not exists observation_offer_idx on core.offer_observation(source, offer_id);
create index if not exists observation_price_idx on core.offer_observation(price);

-- ---------------------------------------------------------------------
-- История цены: каждое изменение — отдельная запись.
--
-- Это представление, а не таблица. Оно выводится из панели оконной функцией
-- и потому не может разъехаться с ней: пересчитать историю цен задним числом
-- нельзя, если таблица была заполнена по ошибочным данным, а представление
-- всегда отражает текущее содержимое панели.
--
-- prev_price берётся LAG по предыдущему НАБЛЮДЕНИЮ, а не по календарному
-- дню: между снимками бывают пропуски (сбой сети, пропущенный запуск), и
-- сравнивать надо с последним, что мы действительно видели.
-- ---------------------------------------------------------------------
create or replace view core.price_change as
with ordered as (
    select source, offer_id, snapshot_date, price
         , lag(price)         over w as prev_price
         , lag(snapshot_date) over w as prev_date
    from core.offer_observation
    where price is not null
      and not is_daily
    window w as (partition by source, offer_id order by snapshot_date)
)
select source
     , offer_id
     , snapshot_date          as changed_on
     , prev_date              as previous_observation
     , snapshot_date - prev_date as days_between_observations
     , prev_price
     , price                  as new_price
     , price - prev_price     as delta_rub
     , round(100.0 * (price - prev_price) / nullif(prev_price, 0), 2) as delta_pct
     , case when price < prev_price then 'down'
            when price > prev_price then 'up'
       end                    as direction
from ordered
where prev_price is not null
  and price <> prev_price;

-- ---------------------------------------------------------------------
-- Фотографии: пока только ссылки и их количество. Перцептивные хеши для
-- дедупликации добавятся на шаге дедупликации — качать 50 тысяч картинок
-- ради первого снимка незачем.
-- ---------------------------------------------------------------------
create table if not exists core.offer_photo (
    source    text not null,
    offer_id  text not null,
    photo_pos smallint not null,
    primary key (source, offer_id, photo_pos),
    url       text not null,
    phash     text,
    foreign key (source, offer_id) references core.offer(source, offer_id) on delete cascade
);
