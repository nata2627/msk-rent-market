-- =====================================================================
-- Объявления, открытые по ссылке из бота (etl/link_fetch.py).
--
-- Отдельная таблица, а не raw.offer_snapshot: пересборка core читает
-- raw.offer_snapshot целиком, и объявление, открытое человеком, попало
-- бы в панель. Панель это то, что видит обход, иначе «новых за сутки» и
-- срок жизни посчитались бы и по тому, на что кто-то случайно наткнулся.
-- Здесь только пополнение, как во всем raw, персональные данные вычищены
-- до записи тем же scrub_payload.
-- =====================================================================
create table if not exists raw.link_snapshot (
    snapshot_id   bigserial   primary key,
    source        text        not null default 'cian',
    offer_id      text        not null,
    fetched_at    timestamptz not null default now(),
    -- Карточка в виде объявления выдачи: страница объявления приведена
    -- к тем же полям, чтобы ее разбирал тот же etl.parse.
    payload       jsonb       not null
);

create index if not exists link_snapshot_offer on raw.link_snapshot (source, offer_id);
