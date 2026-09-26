"""Разбор карточки объявления в строки таблиц.

Модуль намеренно недоверчив к входным данным. Площадка меняет схему молча:
поле может переехать, переименоваться или начать приходить строкой вместо
числа. Поэтому:

* всё достаётся безопасными геттерами, отсутствие поля даёт None, а не падение;
* полный payload всё равно лежит в raw.offer_snapshot, так что любую ошибку
  разбора можно исправить и пересобрать core заново, не теряя данных;
* неизвестные ключи верхнего уровня складываются в unmapped_keys — это
  единственный способ заметить, что площадка начала отдавать что-то новое.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from etl.privacy import hash_seller_id, scrub_text

# ---------------------------------------------------------------------
# Безопасные геттеры
# ---------------------------------------------------------------------
def _get(d, *path, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _num(value, cast=float):
    """Число из чего угодно.

    ЦИАН отдаёт площадь то числом, то строкой '54.0', то строкой '54,0'.
    Молча вернуть None на запятой значило бы потерять площадь у части
    объявлений и перекосить регрессию.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return cast(value)
    if isinstance(value, str):
        cleaned = value.replace(",", ".").replace(" ", "").strip()
        if not cleaned:
            return None
        try:
            return cast(float(cleaned))
        except ValueError:
            return None
    return None


def _ts(value):
    """Unix-время или ISO-строка -> timestamptz."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------
# Животные: четыре состояния
# ---------------------------------------------------------------------
# Сводить к булеву флагу нельзя. «Запрещено» и «не указано» — разные вещи:
# в первом случае хозяин принял решение, во втором мы просто не знаем. Если
# слить их в False, надбавка за разрешённых животных смешается с надбавкой
# за заполненную анкету, а это уже не про животных.
#
# Состояния «по договорённости» в структурированных полях ЦИАНа нет вовсе,
# оно живёт только в тексте описания. Поэтому текст разбирается отдельно.

_ANIMAL = re.compile(r"живот|питом|кошк|собак|\bкот\b|\bкота\b|\bкоты\b", re.I)
_FORBIDDEN = re.compile(r"без\s+живот|без\s+питом|нельзя|не\s+допуск|не\s+разреш|запрещ|не\s+сдаю|без\s+кошек|без\s+собак", re.I)
_NEGOTIABLE = re.compile(r"договорённост|договоренност|согласован|обсужда|рассмотр|по\s+договор", re.I)
_ALLOWED = re.compile(r"можно\s+с|разреш|допуск|приветств|не\s+против", re.I)

# Единица контекста — ПРЕДЛОЖЕНИЕ, а не окно из N символов вокруг слова.
# Так было сначала, и правило ошибалось: в описании «С животными по
# договорённости. Курить в квартире нельзя.» слово «нельзя» попадало в окно
# и превращало разрешение в запрет. Запрет курения к животным отношения не
# имеет, а граница предложения его отсекает.
_SENTENCE = re.compile(r"[.!?;\n]+")


def pets_from_text(description: str | None) -> str | None:
    """Состояние «животных» из текста описания, или None если не сказано."""
    if not description:
        return None

    verdicts = []
    for sentence in _SENTENCE.split(description):
        if not _ANIMAL.search(sentence):
            continue

        # Порядок проверок важен. «Без животных, но можно обсудить» — это
        # всё-таки запрет с оговоркой, а не свободное разрешение, поэтому
        # запрет проверяется первым.
        if _FORBIDDEN.search(sentence):
            verdicts.append("forbidden")
        elif _NEGOTIABLE.search(sentence):
            verdicts.append("negotiable")
        elif _ALLOWED.search(sentence):
            verdicts.append("allowed")

    if not verdicts:
        return None
    # Если в тексте нашлось и разрешение, и запрет, берём консервативное
    # прочтение: лучше недосчитать разрешений, чем приписать их квартире,
    # где животных на самом деле не ждут.
    for state in ("forbidden", "negotiable", "allowed"):
        if state in verdicts:
            return state
    return None


def pets_state(raw_flag, description: str | None) -> tuple[str, bool | None, str]:
    """Итоговое состояние, исходный флаг площадки и источник вывода.

    Источник фиксируется отдельной колонкой, потому что у трёх способов
    узнать про животных разная надёжность, и в регрессии это надо различать:

      'platform_filter' — объявление попало в выдачу с фильтром «можно с
                          животными». Самое твёрдое знание: так его
                          классифицировала сама площадка. Проставляется не
                          здесь, а в сборщике, по разнице двух запросов.
      'platform_field'  — поле petsAllowed в карточке. В выдаче ЦИАНа его
                          нет, но на других источниках может быть.
      'text'            — вычитано из описания. Слабее всего.
      'none'            — не сказано нигде.

    Структурированному полю верим больше, чем тексту: его заполняет сам
    автор объявления в анкете.
    """
    if raw_flag is True:
        return "allowed", True, "platform_field"
    if raw_flag is False:
        return "forbidden", False, "platform_field"
    from_text = pets_from_text(description)
    if from_text:
        return from_text, None, "text"
    return "unknown", None, "none"


# ---------------------------------------------------------------------
# Депозит числом месяцев
# ---------------------------------------------------------------------
# Связка нарочно не пускает через границу предложения и строки.
#
# Прежнее «любые 20 не-цифр» перескакивало на соседнюю фразу: «Залог.
# Договор 11 месяцев» читалось как залог в одиннадцать месячных плат.
# Таких объявлений набралось 37, и у каждого сумма «нужно сразу»
# завышалась в двенадцать раз.
_DEPOSIT_MONTHS = re.compile(
    r"(?:залог|депозит)[^\d\n.;!?,()]{0,20}?(\d{1,2})(?:[.,](\d))?\s*(?:мес|месяц)",
    re.I,
)
# В обратном порядке связка ещё короче: между числом и словом «залог»
# в осмысленной фразе («2 месяца залог») нет ничего, кроме пробела.
# Запятая и скобка означают, что началось другое условие: «договор
# 11 месяцев, залог 50 000» — это срок аренды, а не размер залога.
_DEPOSIT_MONTHS_REVERSED = re.compile(
    r"(\d{1,2})(?:[.,](\d))?\s*мес\w*[ \t-]{0,3}(?:залог|депозит)",
    re.I,
)
# Больше полугода залога не бывает: это всегда вырванный из соседней
# фразы срок договора. Прежняя граница в 12 месяцев пропускала «договор
# 11 месяцев» — самую частую формулировку срока в объявлениях.
_DEPOSIT_MONTHS_MAX = 6


# Связка между словом и числом нарочно короткая: «за», «в размере» — и всё.
# Широкое «любые 20 символов» цепляло соседние предложения: в тексте
# «...при долгосрочной предоплате — при оплате за 3 месяца — 575 000»
# речь про скидку, а не про условие заселения.
_PREPAY_N = re.compile(
    r"предоплат\w*\s*(?:за\s*|в\s+размере\s*)?(\d{1,2})\s*(?:мес|месяц)", re.I)
# Обратный порядок — только в пределах одной строки. Перевод строки
# означает новый пункт списка: «Договор 11 месяцев\nПредоплата за 1-й
# месяц» читалось как предоплата за одиннадцать месяцев.
_PREPAY_N_REVERSED = re.compile(
    r"(\d{1,2})\s*мес\w*\.?[ \t]*предоплат", re.I)
# «первый и последний месяц», «оплата первого и последнего месяцев»,
# «первый месяц + последний месяц».
_FIRST_AND_LAST = re.compile(
    r"перв\w+\s*(?:месяц\w*\s*)?(?:и|\+|,)\s*последн\w+\s*месяц", re.I)
_LAST_MONTH = re.compile(r"последн\w+\s*месяц", re.I)
# «залог за последний месяц» — это депозит, а не вторая арендная плата.
# Без этой оговорки один и тот же месяц посчитался бы дважды.
_DEPOSIT_AS_LAST = re.compile(
    r"(?:залог|депозит)\w*\D{0,15}?за\s+последн\w+\s*месяц", re.I)


# Строка условий, которую площадка собирает сама:
# «Залог 58 000 ₽, комиссия 50%(agent_fee), предоплата 1 месяц, на
# длительный срок». Заполнена у всех 112 289 снимков, предоплата названа
# в 108 487 из них. Это несравнимо надёжнее разбора свободного описания,
# и главное — здесь предоплата и залог перечислены как РАЗНЫЕ условия,
# так что складывать их можно без опаски.
_PREPAY_TERMS = re.compile(r"предоплата\s+(\d{1,2})\s*месяц", re.I)


def prepay_months_from_terms(info: str | None) -> float | None:
    """Предоплата в месяцах из строки условий карточки."""
    if not info:
        return None
    m = _PREPAY_TERMS.search(info)
    if not m:
        return None
    value = float(m.group(1))
    return value if 0 < value <= 12 else None


def prepay_months_from_text(description: str | None) -> float | None:
    """Сколько месяцев аренды нужно отдать при заселении.

    Площадка это поле не отдаёт вовсе (`terms.prepayMonths` пуст у всех
    объявлений до единого), а условие встречается: на снимке 26.09.2026
    «предоплата» упоминается в 409 описаниях, «последний месяц» — в 160.
    Для таких объявлений сумма на входе занижалась ровно на месячную
    плату, и это худший вид ошибки — незаметный и всегда в одну сторону.

    Возвращается None, если в тексте про предоплату ничего нет: это не
    «ноль», а «не сказано», и подставлять вместо него единицу должен тот,
    кто считает, а не тот, кто разбирает.
    """
    if not description:
        return None

    months: float | None = None
    for pattern in (_PREPAY_N, _PREPAY_N_REVERSED):
        m = pattern.search(description)
        if m:
            value = float(m.group(1))
            if 0 < value <= 12:
                months = value
            break

    # «Первый и последний» — два месяца, сколько бы ни было написано про
    # предоплату отдельно: «предоплата за 1 месяц + последний месяц» это
    # именно два, а не один.
    if _FIRST_AND_LAST.search(description):
        months = max(months or 1.0, 2.0)
    elif _LAST_MONTH.search(description) and not _DEPOSIT_AS_LAST.search(description):
        months = max(months or 1.0, 2.0)

    return months


def deposit_months_from_text(description: str | None) -> float | None:
    """Депозит, выраженный в месяцах аренды.

    В анкете депозит задаётся суммой, но в тексте его часто пишут месяцами
    («залог 1 месяц»). Приводить месяцы к рублям прямо здесь нельзя: это
    потеряло бы формулировку условия, а она и есть предмет наблюдения —
    депозит в месяцах автоматически растёт вместе с ценой, а в рублях нет.
    """
    if not description:
        return None
    for pattern in (_DEPOSIT_MONTHS, _DEPOSIT_MONTHS_REVERSED):
        m = pattern.search(description)
        if m:
            whole, frac = m.group(1), m.group(2)
            value = float(f"{whole}.{frac}") if frac else float(whole)
            return value if 0 < value <= _DEPOSIT_MONTHS_MAX else None
    return None


# ---------------------------------------------------------------------
# География
# ---------------------------------------------------------------------
def parse_geo(payload: dict) -> dict:
    """Адресные части, координаты, округ и район.

    Адрес у ЦИАНа приходит списком частей с типами, а не строкой: так
    устроено для построения хлебных крошек. Разбираем по типу, а не по
    позиции — порядок частей у разных объявлений различается.
    """
    geo = _get(payload, "geo", default={}) or {}
    parts = geo.get("address") or []

    named: dict[str, str] = {}
    for part in parts:
        if isinstance(part, dict) and part.get("type") and part.get("name"):
            named.setdefault(str(part["type"]).lower(), part["name"])

    # Район берём из geo.districts, а не из частей адреса: на проверенной
    # выдаче districts заполнен у 28 объявлений из 28, а часть адреса с
    # типом raion — у 27. Разница небольшая, но район это ось всей
    # аналитики, и терять его на ровном месте незачем.
    # Поселение здесь наравне с районом: в Новой Москве административная
    # единица называется именно так, и без неё 1566 объявлений из 16 519
    # остаются без территории — а это не случайная пропажа, Новая Москва
    # заметно дешевле, и её отсутствие сдвинуло бы любую оценку.
    # Микрорайон не берём: он вложен в район, и подменять им район значило
    # бы получить два разных уровня в одном поле.
    districts = geo.get("districts") or []
    district_from_list = next(
        (d.get("name") for d in districts
         if isinstance(d, dict)
         and d.get("type") in ("raion", "district", "poselenie")
         and d.get("name")),
        None,
    )

    coords = geo.get("coordinates") or {}
    address = geo.get("userInput") or ", ".join(
        p["name"] for p in parts if isinstance(p, dict) and p.get("name")
    ) or None

    return {
        "address": address,
        "lat": _num(coords.get("lat")),
        "lon": _num(coords.get("lng") if "lng" in coords else coords.get("lon")),
        "okrug": named.get("okrug"),
        # 'raion' — именно так площадка называет тип части адреса для района.
        "district": (district_from_list or named.get("raion")
                     or named.get("poselenie") or named.get("district")),
        "street": named.get("street"),
        "house": named.get("house"),
    }


def parse_metro(payload: dict) -> list[dict]:
    out = []
    for u in _get(payload, "geo", "undergrounds", default=[]) or []:
        if not isinstance(u, dict) or not u.get("name"):
            continue
        out.append({
            "station_name": u["name"],
            "time_min": _num(u.get("time"), int),
            "transport_type": u.get("transportType"),
            # В выдаче поля lineName нет — есть lineId и lineColor.
            # Приводим к строке: названия линии тут не будет, но
            # идентификатор позволит связать станции одной ветки.
            "line_name": (str(u["lineName"]) if u.get("lineName")
                          else (str(u["lineId"]) if u.get("lineId") is not None else None)),
            # Настоящий метрополитен или наземный диаметр.
            #
            # У метро lineType пуст, у остального заполнен: mck — МЦК,
            # mcd1..mcd4 — диаметры. Различие не косметическое: МЦД ходит
            # по расписанию с интервалом в 10-15 минут, метро — каждые
            # две минуты, и «семь минут до станции» означает совершенно
            # разное время в пути. На последнем снимке у 3117 объявлений
            # ближайшая станция — МЦД-4, а не метро.
            "line_type": u.get("lineType"),
        })
    return out


def metro_walk_min(stations: list[dict], real_metro_only: bool = False) -> int | None:
    """Минимальное время ПЕШКОМ до станции.

    Время на транспорте сюда не попадает намеренно: пятнадцать минут пешком
    и пятнадцать минут на автобусе — разные блага, и смешивать их в одной
    переменной регрессии значило бы мерить не то.

    С real_metro_only считается только метрополитен, без МЦК и диаметров.
    Разница ощутимая: из 8958 объявлений с пешей доступностью до 10 минут
    889 держатся на диаметре, а не на метро, а у 914 пешего метрополитена
    нет вовсе.
    """
    walk = [s["time_min"] for s in stations
            if s.get("transport_type") == "walk" and s.get("time_min") is not None
            and (not real_metro_only or s.get("line_type") is None)]
    return min(walk) if walk else None


# ---------------------------------------------------------------------
# Поля, найденные через журнал нераспознанных ключей
# ---------------------------------------------------------------------
_RANGE = re.compile(r"([\d\s\u00a0]+)\s*[—–-]\s*([\d\s\u00a0]+)")


def parse_price_estimate(good_price) -> dict:
    """Оценка цены самой площадкой.

    Приходит текстом: {"priceLabel": "Хорошая цена",
                       "estimationRange": "52 000—63 000 ₽/мес"}.
    Разбирается в два числа, потому что как строка оно ни с чем не
    сравнивается, а сравнить есть с чем: это оценка чужой модели той же
    величины, которую моделируем мы.
    """
    # Ключи возвращаются всегда, даже пустыми: словарь наблюдения
    # собирается через ** и должен иметь одинаковый набор полей для всех
    # объявлений, иначе запись в базу споткнётся на первом же без оценки.
    out = {"price_label": None, "price_estimate_low": None,
           "price_estimate_high": None}
    if not isinstance(good_price, dict):
        return out

    out["price_label"] = good_price.get("priceLabel")
    m = _RANGE.search(good_price.get("estimationRange") or "")
    if m:
        def num(x):
            return float(re.sub(r"[^\d]", "", x) or 0) or None
        out["price_estimate_low"] = num(m.group(1))
        out["price_estimate_high"] = num(m.group(2))
    return out


def parse_platform_duplicates(identical) -> list[str] | None:
    """Дубли, найденные самой площадкой.

    Подпись у блока — «У других арендодателей», то есть площадка считает
    это одной квартирой у разных продавцов. Ровно то, что ищет наша
    дедупликация, — значит есть с чем сверяться.
    """
    if not isinstance(identical, dict):
        return None
    ids = identical.get("duplicatesOffersIds")
    if not ids:
        return None
    return [str(i) for i in ids]


# ---------------------------------------------------------------------
# Главная функция разбора
# ---------------------------------------------------------------------
# Поля верхнего уровня, которые разбор знает в лицо. Список сверен с живой
# карточкой из архивного снимка выдачи (tests/fixtures/cian_serp_offers.json),
# а не выдуман: всё, чего здесь нет, попадёт в unmapped_keys и будет видно
# в `make checks`.
KNOWN_TOP_LEVEL = {
    # опознание и служебное
    "id", "cianId", "externalId", "fullUrl", "gaLabel", "status", "category",
    "dealType", "offerType", "publishedUserId", "userId", "cianUserId",
    "creationDate", "addedTimestamp", "added", "humanizedTimedelta",
    # квартира
    "roomsCount", "roomsForSaleCount", "bedroomsCount", "flatType",
    "isApartments", "totalArea", "livingArea", "kitchenArea", "roomArea",
    "minArea", "areaParts", "floorNumber", "building", "decoration", "layout",
    "balconiesCount", "loggiasCount", "hasFurniture", "isNeedHideExactAddress",
    # деньги
    "bargainTerms", "formattedFullPrice", "formattedShortPrice",
    "formattedAdditionalInfo", "formattedShortInfo", "formattedFullInfo",
    "discount", "monthlyIncome",
    # текст, фото, автор
    "title", "description", "descriptionMinhash", "descriptionWordsHighlighted",
    "isDuplicatedDescription", "photos", "videos", "user", "phones", "notes",
    # география
    "geo", "jkUrl", "newbuilding",
    # признаки самой площадки: на цену не влияют, в анализе не участвуют
    "accessType", "adfoxParams", "allFromOffrep", "auction", "basicProfiScore",
    "bestPlaceAnalyticsAvailable", "booking", "brandingLevel", "buildersIds",
    "buildingCadastralNumber", "businessShoppingCenter", "cadastralNumber",
    "categoriesIds", "chat", "chatId", "commercialOwnership",
    "comparisonStatus", "coworking", "coworkingOfferType", "dailyrent",
    "dealRentVersion", "demolishedInMoscowProgramm", "factoids",
    "fromDeveloper", "garage", "hasFairPrice", "isAuction",
    "isBookedFromDeveloper", "isByCommercialOwner", "isByHomeowner",
    "isCalltrackingEnabled", "isCianPartner", "isColorized",
    "isCommercialOwnershipVerified", "isDealRequestSubstitutionPhone",
    "isExcludedFromAction", "isExternalTourAvailable", "isFavorite",
    "isImported", "isInHiddenBase", "isLayoutApproved", "isNew", "isNewLabel",
    "isNewbuildingTourAvailable", "isPremium", "isPro", "isRecidivist",
    "isRentByParts", "isRosreestrChecked", "isStandard", "isSubsidisedMortgage",
    "isTop3", "isUnique", "isVerifiedByCian", "kp", "land", "maxAuctionService",
    "moderationInfo", "newbuildingCplLayer", "newbuildingDynamicCalltracking",
    "newbuildingVasPromotion", "newbuildingWithExtendedFeatures",
    "offerFeatureLabels", "offerFeatureLabelsV2", "offerInDeal", "officeType",
    "permittedUseType", "photoFeatureIcons", "promoInfo", "readyBusinessType",
    "rentByPartsDescription", "rosreestrCheck", "savedSearchLabels",
    "shareAmount", "showGalleryThumbnails", "showWarningMessage", "similar",
    "specialty", "tradingLink", "villageMortgageAllowed", "workTimeInfo",
    "workplaceCount",
    # имена, которых в выдаче нет, но которые встречаются на других путях
    "goodPrice", "homeownerProofs", "identicalOffers",
    "searchDuplicatesRotationPromo", "contextToken", "isEarlyAccessEnabled",
    "isInCpd", "isOnlineRentalAgreementEnabled", "mortgagePayment",
    "editDate", "petsAllowed", "childrenAllowed", "repairType",
    "separateWcsCount", "combinedWcsCount", "hasKitchenFurniture", "hasWasher",
    "hasFridge", "hasDishwasher", "hasConditioner", "hasTv", "hasInternet",
    "hasBathtub", "hasShower", "hasPhone",
}


def parse_offer(payload: dict, snapshot_date, source: str = "cian") -> dict | None:
    """Карточка -> словари для core.offer, core.offer_observation и спутников.

    Возвращает None, если у объявления нет идентификатора: без него оно не
    встраивается в панель — нечем связать наблюдения между датами.
    """
    offer_id = payload.get("id") or payload.get("cianId")
    if offer_id is None:
        return None
    offer_id = str(offer_id)

    terms = payload.get("bargainTerms") or {}
    building = payload.get("building") or {}
    user = payload.get("user") or {}
    description = scrub_text(payload.get("description"))

    geo = parse_geo(payload)
    stations = parse_metro(payload)

    lease_term = terms.get("leaseTermType")
    is_daily = (
        lease_term == "daily"
        or terms.get("paymentPeriod") == "day"
        or payload.get("category") in ("dailyFlatRent", "dailyRoomRent")
    )

    state, raw_flag, pets_src = pets_state(payload.get("petsAllowed"), description)

    offer = {
        "source": source,
        "offer_id": offer_id,
        "url": payload.get("fullUrl"),
        # creationDate — первая публикация, addedTimestamp — последний подъём
        # объявления в выдаче. На проверенных данных creationDate разбросан
        # по 2022-2025 годам, а addedTimestamp у всех приходится на последние
        # дни. Перепутать их значит посчитать срок жизни объявления по дате
        # его последнего поднятия, то есть занизить в разы.
        "published_at": _ts(payload.get("creationDate")),
        "rooms_count": _num(payload.get("roomsCount"), int),
        "flat_type": payload.get("flatType"),
        "is_apartments": payload.get("isApartments"),
        "total_area": _num(payload.get("totalArea")),
        "living_area": _num(payload.get("livingArea")),
        "kitchen_area": _num(payload.get("kitchenArea")),
        "floor": _num(payload.get("floorNumber"), int),
        "floors_total": _num(building.get("floorsCount"), int),
        "building_material": building.get("materialType"),
        "build_year": _num(building.get("buildYear"), int),
        "has_lift": bool(_num(_get(building, "passengerLiftsCount"), int) or 0) or None,
        "metro_walk_min": metro_walk_min(stations),
        "metro_real_walk_min": metro_walk_min(stations, real_metro_only=True),
        **geo,
    }

    utilities = terms.get("utilitiesTerms") or {}
    observation = {
        "source": source,
        "offer_id": offer_id,
        "snapshot_date": snapshot_date,
        "price": _num(terms.get("price")),
        "currency": terms.get("currency"),
        "payment_period": terms.get("paymentPeriod"),
        "utilities_included": utilities.get("includedInPrice"),
        "utilities_price": _num(utilities.get("price")),
        "meters_not_included": utilities.get("flowMetersNotIncludedInPrice"),
        "deposit_rub": _num(terms.get("deposit") or terms.get("securityDeposit")),
        "deposit_months": deposit_months_from_text(description),
        "client_fee_pct": _num(terms.get("clientFee")),
        "agent_fee_pct": _num(terms.get("agentFee")),
        "commission_rub": _num(terms.get("commission")),
        # Три источника по убыванию надёжности: анкета (пуста всегда),
        # строка условий, которую площадка собирает сама (заполнена у
        # 96.6% карточек), и в последнюю очередь свободное описание.
        "prepay_months": (_num(terms.get("prepayMonths"), int)
                          or prepay_months_from_terms(
                              payload.get("formattedAdditionalInfo"))
                          or prepay_months_from_text(description)),
        "lease_term_type": lease_term,
        "is_daily": bool(is_daily),
        "pets_state": state,
        "pets_raw": raw_flag,
        "pets_source": pets_src,
        # Проставляется сборщиком по разнице двух запросов: без фильтра и
        # с фильтром «можно с животными». None означает, что проход с
        # фильтром не делался, и судить о животных можно только по тексту.
        "pets_platform_allowed": None,
        "children_allowed": payload.get("childrenAllowed"),
        # В выдаче поле называется decoration, в других местах — repairType.
        "repair_type": payload.get("repairType") or payload.get("decoration"),
        "has_furniture": payload.get("hasFurniture"),
        "has_kitchen_furniture": payload.get("hasKitchenFurniture"),
        "has_washer": payload.get("hasWasher"),
        "has_fridge": payload.get("hasFridge"),
        "has_dishwasher": payload.get("hasDishwasher"),
        "has_conditioner": payload.get("hasConditioner"),
        "has_tv": payload.get("hasTv"),
        "has_internet": payload.get("hasInternet"),
        "has_bathtub": payload.get("hasBathtub"),
        "has_shower": payload.get("hasShower"),
        "balconies_count": _num(payload.get("balconiesCount"), int),
        "loggias_count": _num(payload.get("loggiasCount"), int),
        "separate_wc_count": _num(payload.get("separateWcsCount"), int),
        "combined_wc_count": _num(payload.get("combinedWcsCount"), int),
        "description": description,
        "description_sha1": (
            hashlib.sha1(description.encode("utf-8")).hexdigest() if description else None
        ),
        "seller_type": seller_type(payload, user),
        # Имя автора не сохраняется. У частных риелторов в этом поле стоит
        # их собственное имя, отличить его от названия агентства нечем,
        # а для анализа нужен только ТИП продавца.
        "seller_name": None,
        # Идентификатор — солёным хешем: по нему считается, сколько
        # объявлений у одного автора, но список людей из базы не собрать.
        "seller_id": hash_seller_id(user.get("cianUserId")),
        "seller_offers_count": _num(user.get("offersCount"), int),
        "seller_rating": _num(user.get("personalRating")),
        # experience — это ГОД начала работы ("2019"), а не число лет.
        "seller_experience_since": _num(user.get("experience"), int),
        "is_by_homeowner": payload.get("isByHomeowner"),
        "photos_count": len(payload.get("photos") or []),
        "edited_at": _ts(payload.get("editDate") or payload.get("addedTimestamp")),
        "title": payload.get("title"),
        # Минхеш описания считает сама площадка, и это готовая подпись текста
        # для схлопывания дублей — своя реализация шинглов не понадобится.
        "description_minhash": payload.get("descriptionMinhash"),
        "is_duplicated_description": payload.get("isDuplicatedDescription"),
        "owner_verified": bool(payload.get("homeownerProofs")) or None,
        "platform_duplicate_ids": parse_platform_duplicates(
            payload.get("identicalOffers")),
        **parse_price_estimate(payload.get("goodPrice")),
        "unmapped_keys": sorted(set(payload) - KNOWN_TOP_LEVEL) or None,
    }

    photos = [
        {"photo_pos": i, "url": p.get("fullUrl") or p.get("url")}
        for i, p in enumerate(payload.get("photos") or [])
        if isinstance(p, dict) and (p.get("fullUrl") or p.get("url"))
    ]

    return {"offer": offer, "observation": observation,
            "metro": stations, "photos": photos}


def seller_type(payload: dict, user: dict) -> str | None:
    """Кто сдаёт: собственник, агентство или частный риелтор.

    Тип продавца — одна из объясняющих переменных: собственник и агентство
    назначают цену по-разному, и агентская комиссия меняет реальную
    стоимость входа. Поэтому флаг собственника проверяется первым, а сырой
    тип пользователя остаётся запасным вариантом.
    """
    if payload.get("isByHomeowner") is True:
        return "homeowner"
    raw = user.get("userType") or user.get("accountType")
    if raw:
        return str(raw)
    if user.get("agencyName") or user.get("companyName"):
        return "agency"
    return None
