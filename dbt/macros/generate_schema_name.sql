{# Схема ровно та, что в профиле, без приставки dbt по умолчанию
   «<схема>_<имя>»: иначе витрины разъехались бы по двум схемам. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name or target.schema }}
{%- endmacro %}
