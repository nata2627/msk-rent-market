{# Ключ из нескольких колонок, как уникальные индексы старых витрин.
   NULL в ключе сравнивается как значение: у median_price_by_district
   район бывает пустым, и две такие строки за один день это дубль, хотя
   уникальный индекс Postgres его пропустил бы. Свой тест вместо пакета
   dbt_utils, чтобы проект собирался без похода в сеть за зависимостями. #}
{% test unique_key(model, columns) %}
select {{ columns | join(', ') }}, count(*) as n
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
