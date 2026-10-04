"""Тесты анализа выживаемости.

Проверяется не «считает ли формула», а три вещи, каждая из которых,
будучи сломанной, даёт правдоподобный и неверный ответ:

* объявление не должно числиться в группе риска в возрасте, когда мы на
  него ещё не смотрели (левое усечение);
* объявление, которое висит до сих пор, не должно считаться снятым
  (цензурирование справа);
* снятие после границы наблюдения не должно засчитываться событием,
  потому что в данных оно ещё не проставлено.
"""
from datetime import date

from src.survival import km, median_age, spans


def row(published, first_seen, disappeared=None):
    return {"published": published, "first_seen": first_seen,
            "disappeared": disappeared}


CUTOFF = date(2026, 10, 1)


def test_age_is_counted_from_publication_not_from_first_sighting():
    """Объявление застали двухнедельным. Его возраст — 14, а не 0.

    Если считать от первой встречи, все сроки жизни окажутся занижены
    ровно на то время, что объявление висело до начала панели.
    """
    r = row(date(2026, 9, 8), date(2026, 9, 22), date(2026, 9, 30))
    (entry, exit_, event), = spans([r], CUTOFF)
    assert entry == 14
    assert exit_ == 22
    assert event is True


def test_offer_is_not_at_risk_before_we_saw_it():
    """Главная поправка на левое усечение.

    Объявление вошло в наблюдение в возрасте 10. В возрастах 0-9 оно в
    знаменателе стоять не должно: мы не могли бы заметить его снятие.
    """
    sp = [(10, 20, True)]
    curve = km(sp, max_age=25)
    risk = {age: n for age, n, _, _ in curve}
    assert all(risk[a] == 0 for a in range(0, 10))
    assert risk[10] == 1 and risk[20] == 1
    assert risk[21] == 0


def test_still_published_offer_is_censored_not_counted_as_event():
    """Два объявления, одно снято, второе висит. Дожитие — половина."""
    sp = [(0, 5, True), (0, 5, False)]
    curve = km(sp, max_age=10)
    s_at_5 = next(s for age, _, _, s in curve if age == 5)
    assert abs(s_at_5 - 0.5) < 1e-9


def test_event_after_cutoff_is_not_an_event():
    """Снятие позже границы наблюдения ещё не подтверждено данными.

    Если засчитать его, последние дни кривой провалятся вниз на событиях,
    которых в панели пока нет.
    """
    r = row(date(2026, 9, 20), date(2026, 9, 22), date(2026, 10, 3))
    (entry, exit_, event), = spans([r], CUTOFF)
    assert event is False
    assert exit_ == (CUTOFF - date(2026, 9, 20)).days


def test_broken_rows_are_dropped_not_silently_counted():
    """Первая встреча раньше публикации — мусор, а не наблюдение.

    Такие строки нельзя тихо оставить: отрицательный возраст входа
    испортил бы знаменатель во всех ранних возрастах.
    """
    bad = row(date(2026, 9, 25), date(2026, 9, 22))
    assert spans([bad], CUTOFF) == []


def test_median_is_the_first_age_below_half():
    sp = [(0, 1, True), (0, 2, True), (0, 3, True), (0, 10, False)]
    curve = km(sp, max_age=12)
    assert median_age(curve) == 2


def test_median_is_none_when_curve_never_reaches_half():
    sp = [(0, 5, False)] * 10 + [(0, 3, True)]
    assert median_age(km(sp, max_age=5)) is None


def test_curve_never_rises():
    """Дожитие — невозрастающая функция. Нарушение означает ошибку в
    знаменателе, а не находку о рынке."""
    sp = [(0, 3, True), (2, 9, True), (5, 12, False), (0, 7, True)]
    values = [s for _, _, _, s in km(sp, max_age=15)]
    assert all(b <= a + 1e-12 for a, b in zip(values, values[1:]))
