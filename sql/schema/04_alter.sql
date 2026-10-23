-- =====================================================================
-- Журнал изменений схемы.
--
-- Зачем отдельный файл. Базовые таблицы объявлены через
-- `create table if not exists`, и на уже работающей базе такой оператор
-- просто пропускается — новую колонку он не добавит. Поэтому всё, что
-- появилось после первой версии схемы, живёт здесь отдельными ALTER'ами
-- с `if not exists`. На пустой базе они отработают сразу после создания
-- таблиц, на заполненной — добавят колонки, не тронув данные.
--
-- Дублирования нет: у каждой колонки ровно одно место объявления.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 2026-09-22. Поля, подтверждённые на живой карточке из архивного снимка
-- выдачи. До этого схема опиралась на предполагаемые имена полей.
-- ---------------------------------------------------------------------

alter table core.offer_observation
    -- Заголовок объявления. Меняется вместе с текстом, поэтому в панели,
    -- а не в карточке объявления.
    add column if not exists title text,

    -- Откуда известно про животных. У трёх способов узнать это разная
    -- надёжность, и в регрессии их нельзя смешивать:
    --   platform_filter — объявление попало в выдачу с фильтром «можно
    --                     с животными»; так его классифицировала сама
    --                     площадка, это самое твёрдое знание;
    --   platform_field  — поле petsAllowed в карточке;
    --   text            — вычитано из описания, слабее всего;
    --   none            — не сказано нигде.
    add column if not exists pets_source text
        check (pets_source in ('platform_filter', 'platform_field', 'text', 'none')),

    -- Жёсткая метка площадки: объявление входит в выдачу по фильтру
    -- «можно с животными». NULL означает, что проход с фильтром не
    -- делался, и судить можно только по тексту. Хранится отдельно от
    -- pets_state, чтобы в анализе можно было взять либо твёрдую метку,
    -- либо сводную оценку — и показать, насколько они расходятся.
    add column if not exists pets_platform_allowed boolean,

    -- Минхеш описания, посчитанный самой площадкой. Готовая подпись
    -- текста для схлопывания дублей: своя реализация шинглов не нужна.
    add column if not exists description_minhash jsonb,
    add column if not exists is_duplicated_description boolean,

    -- Автор. experience у площадки — это ГОД начала работы ("2019"),
    -- а не число лет; название колонки об этом говорит прямо.
    add column if not exists seller_rating numeric(4,2),
    add column if not exists seller_experience_since smallint;

create index if not exists observation_pets_idx
    on core.offer_observation(pets_state, pets_source);

-- Корзина, обойденная с фильтром «можно с животными». Такие корзины —
-- разметочные: объявления из них уже собраны обычным проходом, и нужны
-- они только чтобы узнать, кого площадка относит к разрешающим животных.
-- В подсчёте полноты покрытия они не участвуют.
alter table raw.bucket
    add column if not exists pets_filter boolean not null default false;

-- Переопределяется здесь, а не в 01_raw.sql: там колонки pets_filter
-- ещё не существует, файлы применяются по алфавиту.
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
  and not coalesce(pets_filter, false)   -- разметочные корзины полноту не меряют
  and claimed_count is not null
  and collected_count < claimed_count;

-- Режим 'archive': обход сохранённых снимков веб-архива. Отличается от
-- обычного сбора тем, что дата снимка берётся из архива, а не из часов,
-- и что выборка — это то, что кто-то когда-то заархивировал, а не рынок.
alter table raw.collection_run drop constraint if exists collection_run_mode_check;
alter table raw.collection_run add constraint collection_run_mode_check
    check (mode in ('full', 'narrow', 'archive'));

-- Адрес архивного снимка: по нему видно, какими фильтрами была сужена
-- выдача, попавшая в архив. Без этого смещение архивной выборки
-- непрослеживаемо.
alter table raw.bucket
    add column if not exists archive_url text,
    add column if not exists archive_timestamp text;

-- Расстояние до КРУПНОГО парка, отдельно от расстояния до любого.
--
-- Причина: OpenStreetMap метит тегом leisure=park и Измайловский лес, и
-- сквер между двумя домами. Медианный контур в выгрузке по Москве —
-- 0.21 га, то есть меньше футбольного поля, и по такому определению
-- «парк рядом» оказывается у 88% объявлений и не различает ничего.
-- Порог в 10 гектаров отделяет районный парк, куда ходят гулять,
-- от озеленения во дворе.
alter table core.offer
    add column if not exists large_park_distance_m integer;

-- ---------------------------------------------------------------------
-- Внешний ряд Росстата: средняя плата за аренду по регионам, помесячно.
--
-- Зачем он нужен при наличии собственной панели. Панель начинается с
-- сентября 2026 и за несколько недель наберёт полтора месяца — этого
-- хватит, чтобы
-- увидеть, кто снижает цену, но не хватит ни на сезонность, ни на тренд.
-- Росстат даёт тот же показатель с 2015 года.
--
-- Чем он НЕ является: это средняя по обследованию Росстата, а не медиана
-- объявлений. Сравнивать уровни двух источников напрямую нельзя — у них
-- разные выборки и разный способ измерения. Сопоставимы изменения.
-- ---------------------------------------------------------------------
create table if not exists core.rosstat_rent (
    region      text     not null,
    rooms       smallint not null,
    period      date     not null,      -- первое число месяца
    primary key (region, rooms, period),
    price       numeric(12,2) not null,
    loaded_at   timestamptz not null default now()
);

-- Индекс потребительских цен, месяц к предыдущему месяцу.
--
-- Нужен, чтобы отделить рост аренды от общего роста цен. Без дефлятора
-- «рост в 2.3 раза за одиннадцать лет» ничего не значит: за тот же срок
-- подорожало всё. Содержательный вопрос — обогнала ли аренда инфляцию.
create table if not exists core.rosstat_cpi (
    region    text not null,
    period    date not null,
    primary key (region, period),
    mom_pct   numeric(8,3) not null,   -- к предыдущему месяцу, в процентах
    loaded_at timestamptz not null default now()
);

-- Внутри МКАД или за ним.
--
-- Считается по настоящей геометрии кольца из OpenStreetMap, а не по списку
-- районов: список составляется вручную и молча врёт на спорных случаях
-- (Бутово, Солнцево, Некрасовка, Куркино формально Москва, но за МКАД).
alter table core.offer
    add column if not exists inside_mkad boolean;

-- ---------------------------------------------------------------------
-- Дедупликация: одна квартира — несколько объявлений.
--
-- Хозяин отдаёт квартиру нескольким агентствам, каждое выкладывает своё
-- объявление. Для площадки это разные карточки, для рынка — одна
-- квартира. Без схлопывания ликвидные квартиры получают в выборке вес
-- в два-три раза больше реального, и медианы уезжают в их сторону.
--
-- Решение хранится отдельной таблицей, а не флагом в core.offer:
-- дедупликация пересчитывается при изменении правил, и её результат
-- не должен быть перемешан с собранными данными.
-- ---------------------------------------------------------------------
create table if not exists core.offer_duplicate (
    source        text not null,
    offer_id      text not null,
    primary key (source, offer_id),
    -- Представитель группы: объявление, которое остаётся в аналитике.
    canonical_id  text not null,
    -- Насколько похожи описания, 0..1. Не единственный признак решения,
    -- но самый информативный из имеющихся.
    text_similarity numeric(4,3),
    -- По чему совпало: 'характеристики+цена+текст' и т.п. Хранится
    -- словами, чтобы решение можно было оспорить, посмотрев глазами.
    matched_on    text not null,
    group_size    smallint not null,
    decided_at    timestamptz not null default now()
);

create index if not exists offer_duplicate_canonical_idx
    on core.offer_duplicate(source, canonical_id);

-- Пары, похожие на дубли, но не схлопнутые: совпали характеристики,
-- но ни цена, ни текст. Это оценка остаточной доли дублей — величина,
-- которую честный отчёт обязан назвать.
create table if not exists core.duplicate_suspect (
    source     text not null,
    offer_a    text not null,
    offer_b    text not null,
    primary key (source, offer_a, offer_b),
    text_similarity numeric(4,3),
    reason     text not null,
    found_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Поля площадки, которые разбор сначала пропустил.
--
-- Нашлись через unmapped_keys — колонку, куда складываются нераспознанные
-- ключи. Ради этого она и заведена: площадка добавляет поля молча, и без
-- такого журнала заметить пропажу нечем.
-- ---------------------------------------------------------------------
alter table core.offer_observation
    -- Собственная оценка цены площадкой: «Хорошая цена», диапазон
    -- «52 000—63 000 ₽/мес». Готовый ориентир чужой модели — есть с чем
    -- сравнить свою гедонистическую регрессию.
    add column if not exists price_estimate_low  numeric(12,2),
    add column if not exists price_estimate_high numeric(12,2),
    add column if not exists price_label text,

    -- Площадка проверила документы собственника. Признак доверия, и
    -- заодно надёжнее собственного флага isByHomeowner.
    add column if not exists owner_verified boolean,

    -- Дубли, найденные самой площадкой («У других арендодателей»).
    -- Это эталон, по которому проверяется наша дедупликация.
    add column if not exists platform_duplicate_ids jsonb;

-- Какие объявления площадка отдала под фильтром «можно с животными».
--
-- Зачем отдельная таблица. Проход с фильтром намеренно не сохраняет
-- payload повторно: он байт в байт тот же, что уже лежит после основного
-- прохода. Но вместе с payload терялся и сам факт попадания под фильтр —
-- он жил только в core. Стоило пересобрать core из raw, и метка
-- исчезала, причём молча: слой raw объявлен самодостаточным, а по факту
-- таковым не был. Здесь хранится ровно то, чего не хватало, — членство.
create table if not exists raw.pets_hit (
    run_id        bigint not null references raw.collection_run(run_id) on delete cascade,
    bucket_id     bigint references raw.bucket(bucket_id),
    source        text   not null default 'cian',
    offer_id      text   not null,
    snapshot_date date   not null,
    primary key (source, offer_id, snapshot_date, run_id)
);

create index if not exists pets_hit_date_idx on raw.pets_hit (source, snapshot_date);

-- Полнота прохода по дню: если хоть по одной корзине фильтр не сработал,
-- помечать остальные объявления как «не разрешено» нельзя.
alter table raw.collection_run add column if not exists pets_pass_complete boolean;

-- ---------------------------------------------------------------------
-- 2026-09-27. Настоящий метрополитен отдельно от диаметров.
--
-- У станции метро поле lineType пусто, у наземного транспорта заполнено:
-- mck — МЦК, mcd1..mcd4 — диаметры. Различие не косметическое: диаметр
-- ходит по расписанию с интервалом в 10-15 минут, метро — каждые две,
-- и «семь минут до станции» означает совершенно разное время в пути.
-- На снимке 26.09.2026 у 3117 объявлений ближайшая станция — МЦД-4.
-- ---------------------------------------------------------------------
alter table core.offer_metro
    add column if not exists line_type text;

alter table core.offer
    -- Минуты пешком до МЕТРО, без МЦК и диаметров. Из 8958 объявлений с
    -- пешей доступностью до 10 минут 889 держатся на диаметре, а у 914
    -- пешего метрополитена нет вовсе.
    add column if not exists metro_real_walk_min smallint;

-- ---------------------------------------------------------------------
-- 2026-10-04. Деление корзины по недобору — отдельно от обычного.
--
-- Корзина «вся Москва, 4 комнаты» заявила 1231 объявление, в порог
-- деления уложилась, прошла все 44 страницы без единой ошибки и отдала
-- 633 уникальных. Примерно с двадцать третьей страницы площадка начала
-- повторять уже показанное. Теперь такие корзины делятся и проходятся
-- заново, но обычное деление («заранее видно, что не влезет») и деление
-- по недобору («выдача пообещала больше, чем отдала») — разные события,
-- и в was_split они бы слиплись. Без различия нельзя посчитать, как
-- часто выдача врёт.
-- ---------------------------------------------------------------------
alter table raw.bucket
    add column if not exists split_after_underfill boolean not null default false;

comment on column raw.bucket.split_after_underfill is
    'Корзина поделена ПОСЛЕ листания, потому что собрано сильно меньше заявленного';

-- ---------------------------------------------------------------------
-- 2026-10-22. Свежесть данных одной строкой.
--
-- Дашборд десять дней показывал данные дня переезда сбора на Mac mini и
-- молчал об этом: ни одна карточка не говорила, на какой день она
-- смотрит. Заметили только вопросом «а он обновляется?».
--
-- Почему определение здесь, а не в карточке Metabase. Копию базы на
-- макбуке, которую читает дашборд, перезаписывает дамп с Mac mini
-- (`make pull-box`), и всё, что живёт только в копии, при переливе
-- исчезает. В схеме оно едет вместе с базой.
--
-- Почему view, а не materialized: ответ должен быть мгновенно верным.
-- Материализованная свежесть сама нуждалась бы в проверке свежести.
-- ---------------------------------------------------------------------
create or replace view marts.data_freshness as
with m as (
    -- Витрины, которые читают сами вкладки: подбор и рынок. «Ушедшие»
    -- намеренно не берём — снятия цензурированы справа на три дня, и их
    -- отставание это свойство метода, а не беда. Взяли бы — карточка
    -- кричала бы «устарели на 3 дня» каждый здоровый день.
    select least((select max(last_seen_date) from marts.offer_scored),
                 (select max(snapshot_date)  from marts.panel_day)) as mart_day
),
c as (select max(snapshot_date) as collected_day from raw.offer_snapshot),
r as (select max(period) as rosstat_month from core.rosstat_rent),
t as (select (now() at time zone 'Europe/Moscow')::date as today)
select m.mart_day,
       c.collected_day,
       r.rosstat_month,
       t.today - m.mart_day as age_days,

       -- Вердикт словами, а не числом: карточка дашборда показывает
       -- ровно эту строку, и читать её должно быть нечего.
       case
           when m.mart_day is null then 'данных нет'
           when t.today - m.mart_day <= 0 then 'сегодня, ' || to_char(m.mart_day, 'DD.MM')
           when t.today - m.mart_day = 1  then 'вчера, '   || to_char(m.mart_day, 'DD.MM')
           else 'УСТАРЕЛИ НА ' || (t.today - m.mart_day)
                || ' дн. — последний день ' || to_char(m.mart_day, 'DD.MM')
       end
       -- Вторая беда, отдельная от первой: витрины материализованные и
       -- пересобираются отдельным шагом прогона. Шаг падает, сбор идёт
       -- дальше — и «Подбор» показывает позавчерашний рынок при свежем
       -- raw. Именно это и было в копии: raw и core на 12.10, а
       -- offer_scored на 10.10. Переливом такое не лечится.
       || case
              when c.collected_day > m.mart_day
                  then ' · ВИТРИНЫ НЕ ПЕРЕСОБРАНЫ: сбор дошёл до '
                       || to_char(c.collected_day, 'DD.MM')
              else ''
          end as verdict,

       -- У ряда Росстата своя мера: он месячный и публикуется с
       -- задержкой, поэтому отставание в один-два месяца — норма, а не
       -- сигнал. Тревожно от четырёх.
       case
           when r.rosstat_month is null then 'ряда Росстата нет'
           else 'ряд Росстата до ' || to_char(r.rosstat_month, 'MM.YYYY') || ', это '
                || ((date_part('year',  t.today) - date_part('year',  r.rosstat_month)) * 12
                  + (date_part('month', t.today) - date_part('month', r.rosstat_month)))::int
                || ' мес. назад'
       end as rosstat_verdict
  from m, c, r, t;

comment on view marts.data_freshness is
    'Свежесть данных одной строкой: что видит дашборд, насколько это старо и не отстали ли витрины от сбора';

-- ---------------------------------------------------------------------
-- 2026-10-22. Подбор одним определением: marts.offer_pick.
--
-- Бот научился показывать верх подбора в чат, и сразу выяснилось, что
-- голый marts.offer_scored для этого не годится: наверх лезут квартиры
-- за три миллиона. Так и задумано в самой оценке — балл за цену
-- перевёрнут намеренно, дорогое читается как признак качества, — но
-- верхом списка это становится только без отбора.
--
-- Отбор у дашборда был, и он жил в питоне (WHERE в
-- scripts/setup_dashboard.py). Повторить его в боте значило бы завести
-- ВТОРОЕ определение подбора, а в этом проекте такое уже было: запрос
-- `make flats` отбирал по is_my_segment, дашборд этой разметкой не
-- пользовался, и два «моих сегмента» тихо разъехались.
--
-- Поэтому базовые условия отбора живут здесь, а дашборд и бот читают их
-- отсюда. Пользовательские фильтры (цена, площадь, комнаты и прочее)
-- остаются сверху: они разные у разных людей и в разные дни, а эти
-- четыре — не предпочтение, а определение того, что вообще считается
-- вариантом.
-- ---------------------------------------------------------------------
create or replace view marts.offer_pick as
select *
  from marts.offer_scored
 where source = 'cian'
   -- Дубли: одна квартира не должна занимать три строки списка. В
   -- аналитике на вкладке «Рынок» они остаются видны.
   and coalesce(is_duplicate, false) = false
   -- Комната в квартире не квартира: площадь указана всей квартиры, и в
   -- подборе такое объявление выглядит дешёвым и большим.
   and coalesce(is_room_share, false) = false
   -- Посуточная аренда — другой рынок.
   and coalesce(is_daily, false) = false
   and coalesce(lease_term_type, 'longTerm') = 'longTerm'
   and price is not null;

comment on view marts.offer_pick is
    'Что считается вариантом подбора: без дублей, комнат, посуточной и короткой аренды. Общее определение для дашборда и бота';

-- ---------------------------------------------------------------------
-- 2026-10-23. Снимок без повторов: полная карточка пишется, только если
-- она изменилась (etl/snapshot_store.py).
--
-- На машине сбора кончался диск: 12,9 ГБ свободно, raw.offer_snapshot
-- 6,9 ГБ и около 0,33 ГБ прироста в сутки. После вычистки служебного шума
-- 75,6% карточек совпадают со вчерашними. Строка снимка пишется
-- по-прежнему каждый день, на ней держатся счетчики собранного, покрытие и
-- снятия; меняется только то, что в ней лежит.
--
-- Все операции быстрые на таблице любого размера: колонки без значения по
-- умолчанию, снятие not null, ограничения NOT VALID проверяют только новые
-- строки. Старые строки не переписываются и не проверяются: у них у всех
-- payload есть.
-- ---------------------------------------------------------------------
alter table raw.offer_snapshot alter column payload drop not null;
alter table raw.offer_snapshot
    add column if not exists payload_hash text,
    add column if not exists same_as bigint,
    -- Время подъема объявления: меняется почти каждый день, а сведение
    -- (агентство подняло объявление), поэтому хранится отдельно и
    -- восстанавливается точно. jsonb, чтобы вернуть ровно то значение,
    -- которое отдала площадка.
    add column if not exists added_ts jsonb;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'offer_snapshot_same_as_fkey') then
        alter table raw.offer_snapshot add constraint offer_snapshot_same_as_fkey
            foreign key (same_as) references raw.offer_snapshot(snapshot_id) not valid;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'offer_snapshot_payload_or_ref') then
        alter table raw.offer_snapshot add constraint offer_snapshot_payload_or_ref
            check (payload is not null or same_as is not null) not valid;
    end if;
end $$;

-- Карточка дня целиком, как если бы ее записали полностью. Все, кто
-- читает payload (пересборка core, справочник районов, условия платежа),
-- читают отсюда. Шумовые поля у восстановленной карточки из базового
-- снимка, см. etl/snapshot_store.py.
create or replace view raw.offer_snapshot_full as
select s.snapshot_id, s.run_id, s.bucket_id, s.source, s.offer_id,
       s.snapshot_date, s.fetched_at, s.page,
       coalesce(s.payload,
                case when s.added_ts is null then b.payload - 'addedTimestamp'
                     else jsonb_set(b.payload, '{addedTimestamp}', s.added_ts) end)
           as payload
  from raw.offer_snapshot s
  left join raw.offer_snapshot b on b.snapshot_id = s.same_as;

comment on view raw.offer_snapshot_full is
    'Снимки с восстановленной карточкой: для строк-ссылок payload собирается из базового снимка и added_ts';
