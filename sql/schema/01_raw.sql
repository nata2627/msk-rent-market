-- =====================================================================
-- Слой 1: сырые снимки.
--
-- Правило слоя: сюда только добавляют. Ничего не перезаписывается и не
-- удаляется. Если завтра выяснится, что разбор поля был неверным, мы
-- перечитаем payload и пересоберём core заново — исходник никуда не делся.
-- Поэтому payload хранится целиком, как пришёл от площадки.
-- =====================================================================

create schema if not exists raw;

-- ---------------------------------------------------------------------
-- Один запуск сбора.
-- ---------------------------------------------------------------------
create table if not exists raw.collection_run (
    run_id              bigserial primary key,
    source              text        not null default 'cian',
    -- 'full'   — еженедельный полный обход всей Москвы,
    -- 'narrow' — ежедневный узкий срез по сегменту заказчицы.
    mode                text        not null check (mode in ('full', 'narrow')),
    -- Дата снимка. Ключ панели — (объявление, дата снимка), поэтому дата
    -- фиксируется один раз на весь запуск: обход идёт полтора часа и может
    -- пересечь полночь, но снимок должен остаться одним снимком.
    snapshot_date       date        not null,
    started_at          timestamptz not null default now(),
    finished_at         timestamptz,
    status              text        not null default 'running'
                                    check (status in ('running', 'ok', 'failed', 'aborted')),
    -- Диагностика полноты обхода: сколько корзин обработано и по скольким
    -- собрано меньше, чем заявил ЦИАН.
    buckets_total       integer,
    buckets_underfilled integer,
    offers_seen         integer,
    requests_made       integer,
    note                text
);

comment on column raw.collection_run.snapshot_date is
    'Дата снимка панели. Одна на весь запуск, даже если обход пересёк полночь.';

-- ---------------------------------------------------------------------
-- Корзина поиска.
--
-- ЦИАН не отдаёт выдачу глубже нескольких десятков страниц, сколько бы
-- объявлений ни нашлось. Поэтому пространство поиска нарезается на корзины
-- (округ -> район -> комнатность -> диапазон цены) так, чтобы каждая
-- помещалась в предел выдачи. Эта таблица — журнал нарезки: по ней видно,
-- где обход недобрал, и она же доказывает полноту покрытия.
--
-- ВАЖНО: диапазоны цены здесь — технический способ нарезки, а не фильтр
-- отбора. Они идут встык и покрывают шкалу от нуля до бесконечности.
-- ---------------------------------------------------------------------
create table if not exists raw.bucket (
    bucket_id        bigserial primary key,
    run_id           bigint      not null references raw.collection_run(run_id) on delete cascade,

    -- Координаты корзины в пространстве поиска. NULL = ось не задействована.
    okrug            text,
    district         text,
    district_id      integer,
    rooms            text,        -- как передано в запрос: '1', '2', 'studio', ...
    price_min        bigint,      -- включительно
    price_max        bigint,      -- включительно; NULL = верхней границы нет

    -- Дерево дробления: какая корзина породила эту и на какой глубине.
    parent_bucket_id bigint references raw.bucket(bucket_id),
    depth            integer     not null default 0,

    -- Результат обхода корзины.
    claimed_count    integer,     -- сколько объявлений заявил ЦИАН
    collected_count  integer,     -- сколько мы реально забрали
    pages_fetched    integer,
    -- Корзина упёрлась в предел выдачи и была разделена: сама она объявления
    -- не собирала, за неё это сделали потомки.
    was_split        boolean     not null default false,
    -- Корзину не удалось разделить (оси кончились), и она всё ещё больше
    -- предела. Это честно зафиксированная дыра в покрытии.
    is_truncated     boolean     not null default false,

    query_json       jsonb       not null,   -- запрос как отправлен, для воспроизводимости
    error            text,
    created_at       timestamptz not null default now()
);

create index if not exists bucket_run_idx on raw.bucket(run_id);

-- Недобор: забрали меньше, чем заявлено, и корзина при этом не делилась.
-- Расхождение — сигнал, что корзина недобрана (пагинация уехала, ошибка сети).
-- Пересоздаётся, а не заменяется: представление построено через select *,
-- и когда в raw.bucket добавляется колонка, `create or replace view` падает
-- с «cannot change name of view column». Для диагностического представления
-- звёздочка удобна — пусть подхватывает новые колонки сама, — поэтому
-- платим за это явным drop.
drop view if exists raw.bucket_underfilled;
create or replace view raw.bucket_underfilled as
select *
     , claimed_count - collected_count as missing
from raw.bucket
where not was_split
  and claimed_count is not null
  and collected_count < claimed_count;

-- ---------------------------------------------------------------------
-- Сырой снимок одного объявления.
--
-- Уникальности здесь намеренно НЕТ. Повторный запуск в тот же день добавит
-- новые строки — это честно, это второй снимок того же дня. Идемпотентность
-- обеспечивается слоем core, который схлопывает их в одно наблюдение.
-- ---------------------------------------------------------------------
create table if not exists raw.offer_snapshot (
    snapshot_id   bigserial primary key,
    run_id        bigint      not null references raw.collection_run(run_id) on delete cascade,
    bucket_id     bigint references raw.bucket(bucket_id),
    source        text        not null default 'cian',
    offer_id      text        not null,
    snapshot_date date        not null,
    fetched_at    timestamptz not null default now(),
    page          integer,
    payload       jsonb       not null
);

create index if not exists offer_snapshot_key_idx
    on raw.offer_snapshot(source, offer_id, snapshot_date);
create index if not exists offer_snapshot_run_idx
    on raw.offer_snapshot(run_id);
