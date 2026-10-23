{{ config(materialized='view') }}

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
with m as (
    -- Витрины, которые читают сами вкладки: подбор и рынок. «Ушедшие»
    -- намеренно не берём — снятия цензурированы справа на три дня, и их
    -- отставание это свойство метода, а не беда. Взяли бы — карточка
    -- кричала бы «устарели на 3 дня» каждый здоровый день.
    select least((select max(last_seen_date) from {{ ref('offer_scored') }}),
                 (select max(snapshot_date)  from {{ ref('panel_day') }})) as mart_day
),
c as (select max(snapshot_date) as collected_day from {{ source('raw', 'offer_snapshot') }}),
r as (select max(period) as rosstat_month from {{ source('core', 'rosstat_rent') }}),
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
  from m, c, r, t
