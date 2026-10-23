-- =====================================================================
-- Слой ml: оценки модели справедливой цены для бота и витрины.
--
-- Отдельная схема, потому что это не данные, а вывод модели: он
-- пересчитывается целиком раз в неделю, и его можно снести без потерь.
-- raw и core модель не трогает. Пишет src/price_model.py, читает бот
-- (/price) обычным SQL: в пути ответа бота нет ни Python-библиотек, ни
-- модели, только таблица.
-- =====================================================================
create schema if not exists ml;

-- Оценка по каждому объявлению, бывшему в последней выдаче.
create table if not exists ml.price_estimate (
    offer_id        text primary key,
    model_version   text not null,
    estimated_on    date not null,
    rooms           smallint,
    district        text,
    total_area      numeric(8,1),
    ask             numeric(12,0) not null,
    estimate        numeric(12,0) not null,
    lo80            numeric(12,0) not null,
    hi80            numeric(12,0) not null,
    exit_estimate   numeric(12,0),
    cian_low        numeric(12,0),
    cian_high       numeric(12,0),
    typical_days    smallint
);

-- Оценка по словам «2к 45 м2 Хамовники»: типичная квартира района с
-- такой комнатностью и площадью, прочие признаки по медиане района.
create table if not exists ml.price_grid (
    district        text not null,
    rooms           smallint not null,
    area_m2         smallint not null,
    model_version   text not null,
    estimate        numeric(12,0) not null,
    lo80            numeric(12,0) not null,
    hi80            numeric(12,0) not null,
    comparable_n    integer not null,
    primary key (district, rooms, area_m2)
);

-- Замер дрейфа: ошибка модели прошлой недели на объявлениях этой.
create table if not exists ml.price_drift (
    week_start      date primary key,
    model_version   text not null,
    n               integer not null,
    mae_rub         integer not null,
    mape_pct        numeric(6,2) not null,
    threshold_pct   numeric(6,2)
);
