"""Схлопывание дублей: одна квартира — несколько объявлений.

    python -m src.dedup                 # посчитать и записать
    python -m src.dedup --report        # только показать, ничего не писать

Зачем. Хозяин отдаёт квартиру нескольким агентствам, каждое выкладывает
своё объявление со своим текстом и своей ценой. Для площадки это разные
карточки, для рынка — одна квартира. Считая медиану по объявлениям, мы
неявно считаем, что строка — это квартира; но дублируются чаще ликвидные
варианты, и они получают вес в два-три раза больше реального.

Главный принцип — КОНСЕРВАТИВНОСТЬ. Лучше не схлопнуть сомнительную пару,
чем склеить разные квартиры. Пропущенный дубль даёт небольшое смещение;
склеенные разные квартиры — это удалённые наблюдения, то есть потеря
данных, а её уже ничем не исправить.

Как устроено решение.

1. **Разбиение на блоки.** Сравнивать все пары нельзя: 25 тысяч объявлений
   это 316 миллионов пар. Сравниваем только внутри одного дома —
   координаты, округлённые до четвёртого знака (около 11 метров). На наших
   данных это 8 755 домов и 31 тысяча пар вместо 316 миллионов.

2. **Характеристики обязательны.** Комнатность, площадь в пределах метра,
   этаж. Без их совпадения пара не рассматривается вовсе.

3. **Но их одних мало.** В доме бывают две одинаковые квартиры на одном
   этаже — в типовых сериях это норма. Поэтому нужен ещё хотя бы один
   признак: совпадение цены или похожий текст.

Пороги не выдуманы, а взяты из данных. На снимке за 2026-09-23 среди
31 156 пар внутри домов:

    характеристики + цена + текст      40   -> схлопываем
    характеристики + цена             295   -> схлопываем
    характеристики + текст              7   -> схлопываем
    только характеристики             331   -> НЕ схлопываем, считаем
    только цена                     2 082   -> совпадение круглых цен
    только текст                      178   -> шаблоны новостроек
    ничего                         28 175

Строка «только текст» показывает, почему текст сам по себе не годится:
178 пар с почти одинаковым описанием — это застройщик, разместивший
типовой текст на разные квартиры. Склеивать их было бы грубой ошибкой.
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from collections import Counter, defaultdict

import psycopg

from etl.config import DB

log = logging.getLogger("dedup")

# Точность блока: четвёртый знак широты и долготы — это примерно 11 метров.
# Меньше — и один дом разъедется на несколько блоков из-за разнобоя в
# координатах у разных агентств; больше — и в один блок попадут соседние дома.
COORD_PRECISION = 4

# Допуск по площади. Агентства округляют по-разному: 54.3 против 54.
AREA_TOLERANCE = 1.0

# Порог похожести описаний. Взят по распределению: оно двугорбое, основная
# масса пар лежит ниже 0.3, а совпадающие тексты — выше 0.8. Промежуток
# между ними почти пуст, так что порог не балансирует на грани.
TEXT_THRESHOLD = 0.8


# ---------------------------------------------------------------------
def minhash_similarity(a: list[int] | None, b: list[int] | None) -> float:
    """Доля совпавших позиций двух подписей описания.

    Минхеш считает сама площадка и отдаёт в поле descriptionMinhash —
    32 числа. Доля совпадающих позиций оценивает меру Жаккара между
    множествами слов, то есть похожесть текстов, и делает это за 32
    сравнения вместо разбора самих текстов.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def attributes_match(a: dict, b: dict) -> bool:
    """Совпадают ли физические характеристики квартиры.

    Обязательное условие: без него пара не рассматривается. Площадь с
    допуском в метр, комнатность и этаж — точно.
    """
    if a["rooms_count"] != b["rooms_count"]:
        return False
    if a["floor"] != b["floor"] or a["floor"] is None:
        return False
    if a["total_area"] is None or b["total_area"] is None:
        return False
    return abs(float(a["total_area"]) - float(b["total_area"])) <= AREA_TOLERANCE


def decide(a: dict, b: dict) -> tuple[str, str, float]:
    """Вердикт по паре: 'duplicate' | 'suspect' | 'different'.

    Возвращает также словесное описание совпавшего — чтобы решение можно
    было оспорить, посмотрев на пару глазами, а не доверять числу.
    """
    similarity = minhash_similarity(a["minhash"], b["minhash"])

    if not attributes_match(a, b):
        return "different", "", similarity

    same_price = (a["price"] is not None and a["price"] == b["price"])
    same_text = similarity >= TEXT_THRESHOLD

    if same_price and same_text:
        return "duplicate", "характеристики+цена+текст", similarity
    if same_price:
        return "duplicate", "характеристики+цена", similarity
    if same_text:
        return "duplicate", "характеристики+текст", similarity

    # Характеристики совпали, но больше ничего. В типовых сериях две
    # одинаковые квартиры на одном этаже — обычное дело, и схлопывать их
    # нельзя. Но и молчать нельзя: это оценка остаточной доли дублей.
    return "suspect", "только характеристики", similarity


# ---------------------------------------------------------------------
class UnionFind:
    """Объединение пар в группы.

    Нужно потому, что дубли бывают не парами: одна квартира может висеть
    у трёх агентств, и тогда пары (A,B), (B,C) обязаны дать одну группу
    из трёх, а не две группы по две.
    """

    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Представителем становится меньший идентификатор — так
            # результат не зависит от порядка обработки пар.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self.parent[hi] = lo

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return {k: sorted(v) for k, v in out.items() if len(v) > 1}


# ---------------------------------------------------------------------
def load(conn, snapshot_date=None) -> list[dict]:
    where = "and ob.snapshot_date = %s" if snapshot_date else """
        and ob.snapshot_date = (select max(snapshot_date) from raw.collection_run
                                where source='cian' and status='ok')"""
    params = (snapshot_date,) if snapshot_date else ()
    with conn.cursor() as cur:
        cur.execute(f"""
            select o.offer_id, o.lat, o.lon, o.rooms_count, o.total_area, o.floor,
                   o.district, ob.description_minhash, ob.price, ob.seller_name,
                   o.first_seen_date
            from core.offer o
            join core.offer_observation ob using (source, offer_id)
            where o.source = 'cian' and o.lat is not null {where}
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["minhash"] = r.pop("description_minhash")
    return rows


def find_duplicates(rows: list[dict]) -> tuple[UnionFind, list[tuple], Counter]:
    """Разбить на блоки, сравнить пары внутри блока, собрать группы."""
    blocks: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (round(float(r["lat"]), COORD_PRECISION),
               round(float(r["lon"]), COORD_PRECISION))
        blocks[key].append(r)

    uf = UnionFind()
    suspects: list[tuple] = []
    stats = Counter()
    reasons: dict[tuple[str, str], tuple[str, float]] = {}

    for group in blocks.values():
        for a, b in itertools.combinations(group, 2):
            stats["пар рассмотрено"] += 1
            verdict, matched_on, similarity = decide(a, b)
            if verdict == "duplicate":
                stats[matched_on] += 1
                uf.union(a["offer_id"], b["offer_id"])
                reasons[(a["offer_id"], b["offer_id"])] = (matched_on, similarity)
            elif verdict == "suspect":
                stats["подозрительных, не схлопнуто"] += 1
                lo, hi = sorted((a["offer_id"], b["offer_id"]))
                suspects.append((lo, hi, round(similarity, 3), matched_on))

    stats["домов"] = len(blocks)
    return uf, suspects, stats, reasons


def save(conn, uf: UnionFind, suspects: list[tuple], reasons: dict) -> int:
    groups = uf.groups()
    rows = []
    for canonical, members in groups.items():
        for offer_id in members:
            if offer_id == canonical:
                matched, sim = "представитель группы", None
            else:
                matched, sim = reasons.get((canonical, offer_id)) \
                    or reasons.get((offer_id, canonical)) or ("через цепочку", None)
            rows.append(("cian", offer_id, canonical,
                         sim, matched, len(members)))

    with conn.cursor() as cur:
        cur.execute("delete from core.offer_duplicate where source='cian'")
        cur.execute("delete from core.duplicate_suspect where source='cian'")
        cur.executemany("""
            insert into core.offer_duplicate
                (source, offer_id, canonical_id, text_similarity, matched_on, group_size)
            values (%s, %s, %s, %s, %s, %s)""", rows)
        cur.executemany("""
            insert into core.duplicate_suspect
                (source, offer_a, offer_b, text_similarity, reason)
            values ('cian', %s, %s, %s, %s)
            on conflict do nothing""", suspects)
    conn.commit()
    return len(groups)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Схлопывание дублей объявлений")
    parser.add_argument("--report", action="store_true",
                        help="только показать результат, ничего не записывать")
    parser.add_argument("--snapshot-date", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with psycopg.connect(**DB) as conn:
        rows = load(conn, args.snapshot_date)
        log.info("Объявлений в снимке: %d", len(rows))
        uf, suspects, stats, reasons = find_duplicates(rows)
        groups = uf.groups()
        duplicates = sum(len(v) - 1 for v in groups.values())

        print()
        for k, v in stats.most_common():
            print(f"  {k:<36} {v:>8}")
        print()
        print(f"  Групп дублей:                        {len(groups):>8}")
        print(f"  Лишних объявлений (схлопывается):    {duplicates:>8}")
        print(f"  Это доля от выборки:                 {100*duplicates/len(rows):>7.1f}%")
        print(f"  Остаточная оценка (не схлопнуто):    "
              f"{100*len(suspects)/len(rows):>7.1f}%")
        if groups:
            biggest = max(groups.values(), key=len)
            print(f"  Самая большая группа:                {len(biggest):>8} объявлений")

        if not args.report:
            n = save(conn, uf, suspects, reasons)
            log.info("Записано групп: %d", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
