"""Справочник районов Москвы — из уже собранных страниц, а не из запроса.

Зачем понадобилось. Нарезка полного обхода начинается с районов: одна
корзина на всю Москву в предел выдачи не влезает, и запрос по ней был бы
потрачен впустую. Справочник брался служебным эндпоинтом
`geo-temp-layer/v1/get-districts-tree`, но он отвечает 404 — служба у
площадки переехала. Браузерный транспорт справочник и не пытался брать,
поэтому полный обход всё это время шёл по комнатности и цене: покрытие
полное, но запросов заметно больше.

Искать новый эндпоинт не понадобилось. Каждое объявление в выдаче несёт
свою географию целиком — `geo.districts` с идентификаторами, именами,
типом узла и ссылкой на родителя, ровно теми `id`, которые площадка
ждёт в параметре `district[0]`. То есть справочник уже лежит в raw, и
собрать его можно вообще без обращения к площадке.

Два решения, которые стоит защищать:

* **Листья — только `raion` и `poselenie`.** Есть ещё `mikroraion`, но он
  вложен в район: взяв его в нарезку, мы бы запросили одни и те же
  объявления дважды — в районе и в микрорайоне внутри него.
* **Лист без округа — не район.** Площадка отдаёт ещё и зонтичные узлы
  вроде «Новой Москвы» (тип `raion`, родителя нет), покрывающие сразу
  целый округ. Проверено: у каждого объявления с такой меткой рядом
  стоит настоящее поселение, так что зонтик ничего не добавляет — но
  как корзина он заставил бы обойти всю Новую Москву второй раз.
* **Файл только пополняется.** Район, в котором сегодня нет ни одного
  объявления, в сегодняшней выдаче не встретится. Выбросить его из
  справочника значило бы перестать его спрашивать — и не заметить, когда
  объявления там появятся. Поэтому старые записи сохраняются, новые
  добавляются.
"""
from __future__ import annotations

import argparse
import json
import logging

from etl import config, db

log = logging.getLogger("districts")

CACHE = config.REFERENCE / "moscow_districts.json"

# Уровни, по которым имеет смысл нарезать обход. Микрорайон намеренно не
# здесь: он лежит внутри района, и его объявления уже посчитаны.
LEAF_TYPES = ("raion", "poselenie")
OKRUG_TYPE = "okrug"

# Сколько административных единиц ожидаем увидеть. В Москве 125 районов
# и 21 поселение (плюс два района Зеленограда, которые площадка тоже
# отдаёт как raion). Цифра нужна не для проверки на равенство, а чтобы
# заметить, если справочник вдруг собрался наполовину.
EXPECTED_LEAVES = 140

SQL = """
select d->>'id'       as id,
       d->>'name'     as name,
       d->>'type'     as type,
       d->>'parentId' as parent_id,
       count(*)       as offers
from raw.offer_snapshot,
     lateral jsonb_array_elements(payload->'geo'->'districts') d
where d ? 'id' and d ? 'name'
group by 1, 2, 3, 4
"""


def collect_nodes(conn) -> dict[int, dict]:
    """Все географические узлы, встреченные в собранных страницах."""
    nodes: dict[int, dict] = {}
    with conn.cursor() as cur:
        cur.execute(SQL)
        for node_id, name, node_type, parent_id, offers in cur.fetchall():
            key = int(node_id)
            node = nodes.setdefault(key, {
                "id": key, "name": name, "type": node_type,
                "parent_id": int(parent_id) if parent_id else None,
                "offers": 0,
            })
            node["offers"] += int(offers)
    return nodes


def okrug_of(node: dict, nodes: dict[int, dict]) -> str | None:
    """Округ, в котором лежит узел.

    Идём вверх по parentId, а не берём родителя напрямую: микрорайон лежит
    в районе, район — в округе, и глубина не гарантирована. Счётчик шагов
    защищает от кольца в данных: справочник важнее, чем зависший обход.
    """
    seen: set[int] = set()
    current = node
    for _ in range(5):
        parent_id = current.get("parent_id")
        if parent_id is None or parent_id in seen:
            return None
        seen.add(parent_id)
        parent = nodes.get(parent_id)
        if parent is None:
            return None
        if parent["type"] == OKRUG_TYPE:
            return parent["name"]
        current = parent
    return None


def build(nodes: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """Единицы нарезки и список отброшенных зонтичных узлов."""
    out, umbrellas = [], []
    for node in nodes.values():
        if node["type"] not in LEAF_TYPES:
            continue
        okrug = okrug_of(node, nodes)
        if okrug is None:
            # Настоящий район всегда лежит в округе. Узел без округа —
            # зонтик над целым округом, брать его в нарезку нельзя.
            umbrellas.append(node["name"])
            continue
        out.append({
            "id": node["id"],
            "name": node["name"],
            "okrug": okrug,
            "type": node["type"],
        })
    return sorted(out, key=lambda d: (d["okrug"], d["name"])), sorted(umbrellas)


def merge_with_cache(fresh: list[dict]) -> tuple[list[dict], int]:
    """Слить с тем, что уже лежит в файле. Ничего не теряя."""
    known: dict[int, dict] = {}
    if CACHE.exists():
        for item in json.loads(CACHE.read_text(encoding="utf-8")):
            known[int(item["id"])] = item

    added = 0
    for item in fresh:
        if item["id"] not in known:
            added += 1
        known[item["id"]] = item

    merged = sorted(known.values(), key=lambda d: (d.get("okrug") or "", d["name"]))
    return merged, added


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что получилось, но файл не трогать")
    args = parser.parse_args()

    with db.connect() as conn:
        nodes = collect_nodes(conn)

    districts, umbrellas = build(nodes)
    by_type: dict[str, int] = {}
    for d in districts:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    okrugs = sorted({d["okrug"] for d in districts})

    log.info("Узлов в собранных страницах: %d", len(nodes))
    log.info("Единиц нарезки: %d (%s)", len(districts),
             ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())))
    log.info("Округов: %d — %s", len(okrugs), ", ".join(okrugs))
    if umbrellas:
        log.info("Отброшено зонтичных узлов: %d — %s "
                 "(их объявления достаются через настоящие районы)",
                 len(umbrellas), ", ".join(umbrellas))
    if len(districts) < EXPECTED_LEAVES:
        log.warning("Ожидалось около %d единиц, собрано %d. "
                    "Справочник неполный — полный обход по нему пропустит район.",
                    EXPECTED_LEAVES, len(districts))

    if args.dry_run:
        log.info("Холостой прогон, файл не записан.")
        return 0

    merged, added = merge_with_cache(districts)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Записано в %s: всего %d, из них новых %d", CACHE, len(merged), added)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
