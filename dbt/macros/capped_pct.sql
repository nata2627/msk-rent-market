-- Доля в процентах, не больше ста. least() в Postgres пропускает NULL,
-- и least(100, x / null) дал бы ровно сто вместо пустоты.
{% macro capped_pct(num, den) -%}
case when nullif({{ den }}, 0) is not null
     then least(100, round(100.0 * ({{ num }}) / ({{ den }}), 1)) end
{%- endmacro %}
