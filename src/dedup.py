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

# Допуск по площади: не меньше метра, но и не меньше 5% от размера.
#
# Плоский допуск в метр был слишком жёстким и терял настоящие дубли:
# у пар, которые площадка сама объявила одной квартирой, медианное
# расхождение площади составило 4 м². Агентства указывают площадь
# по-разному — кто-то с лоджией, кто-то без, — и расхождение растёт
# вместе с размером квартиры, поэтому допуск относительный.
#
# Значения подобраны по эталону, а не назначены. Доля найденных из тех,
# что площадка объявила дублями сама:
#
#     ±1 м²           52.0%   схлопывает  967 пар
#     ±1 м² или 3%    54.7%               1059
#     ±1 м² или 5%    61.1%               1173   <- взято
#     ±5 м²           62.8%               1295
#
# Последняя строка даёт почти ту же полноту, но схлопывает на 122 пары
# больше: плоский допуск в пять метров склеивает маленькие квартиры,
# которые различаются по-настоящему.
AREA_TOLERANCE_ABS = 1.0
AREA_TOLERANCE_REL = 0.05

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
    x, y = float(a["total_area"]), float(b["total_area"])
    allowed = max(AREA_TOLERANCE_ABS, AREA_TOLERANCE_REL * max(x, y))
    return abs(x - y) <= allowed


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
    """Объявления для сверки: по последнему наблюдению каждого.

    Не по одному снимку, и это принципиально. Дубль — свойство квартиры,
    а не дня: объявление, которое висело 22-го и пропало 23-го, остаётся
    дублем того, что висит до сих пор. Ограничение одним снимком теряло
    такие пары — на эталоне площадки полнота падала с 61% до 21%, просто
    потому что одна сторона пары в этот день не наблюдалась.

    snapshot_date оставлен для воспроизведения расчёта на конкретную дату.
    """
    if snapshot_date:
        where, params = "and ob.snapshot_date = %s", (snapshot_date,)
    else:
        where, params = "", ()
    with conn.cursor() as cur:
        cur.execute(f"""
            select distinct on (o.offer_id)
                   o.offer_id, o.lat, o.lon, o.rooms_count, o.total_area, o.floor,
                   o.district, ob.description_minhash, ob.price, ob.seller_name,
                   o.first_seen_date
            from core.offer o
            join core.offer_observation ob using (source, offer_id)
            where o.source = 'cian' and o.lat is not null {where}
            order by o.offer_id, ob.snapshot_date desc
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


def validate_against_platform(conn) -> None:
    """Сверить нашу дедупликацию с той, что делает сама площадка.

    У ЦИАНа есть собственное определение дублей — блок «У других
    арендодателей» в карточке. Это не полная истина: там только те пары,
    которые нашла площадка, и наши находки сверх этого списка вполне могут
    быть настоящими. Поэтому меряется ПОЛНОТА — какую долю площадочных пар
    находим мы, — а доля «лишних» как ошибка не трактуется.
    """
    with conn.cursor() as cur:
        cur.execute("""select distinct ob.offer_id, d
                       from core.offer_observation ob,
                            lateral jsonb_array_elements_text(ob.platform_duplicate_ids) d
                       where ob.source='cian' and ob.platform_duplicate_ids is not null""")
        platform = {tuple(sorted(r)) for r in cur.fetchall() if r[0] != r[1]}
        cur.execute("select offer_id from core.offer where source='cian'")
        known = {r[0] for r in cur.fetchall()}
        cur.execute("select canonical_id, offer_id from core.offer_duplicate where source='cian'")
        groups: dict[str, set] = {}
        for canon, oid in cur.fetchall():
            groups.setdefault(canon, set()).add(oid)

    ours = set()
    for members in groups.values():
        ours |= {tuple(sorted(p)) for p in itertools.combinations(sorted(members), 2)}
    truth = {p for p in platform if p[0] in known and p[1] in known}
    if not truth:
        print("\n  Эталонных пар от площадки нет — сверять не с чем.")
        return

    hit = len(ours & truth)
    print(f"\n  Сверка с дедупликацией самой площадки:")
    print(f"    пар объявлено площадкой:           {len(platform):>6}")
    print(f"    из них обе стороны есть у нас:     {len(truth):>6}")
    print(f"    нашли и мы:                        {hit:>6}  ({100*hit/len(truth):.1f}%)")
    print(f"    нашли мы, площадка молчит:         {len(ours - truth):>6}")
    print(f"    пропустили:                        {len(truth - ours):>6}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Схлопывание дублей объявлений")
    parser.add_argument("--report", action="store_true",
                        help="только показать результат, ничего не записывать")
    parser.add_argument("--validate", action="store_true",
                        help="сверить с дедупликацией самой площадки")
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
            if args.validate:
                validate_against_platform(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
