"""Снимок без повторов: что считается шумом и что нет, и как пишет
save_snapshots. База подменяется курсором, который запоминает запросы."""
from etl import db
from etl.snapshot_store import payload_hash, restore

BASE = {
    "id": 1, "fullUrl": "https://www.cian.ru/rent/flat/1/?context=AAA&mlSearchSessionGuid=1",
    "contextToken": "x", "humanizedTimedelta": "2 часа назад", "added": "вчера",
    "addedTimestamp": 100, "title": "Двушка",
    "bargainTerms": {"price": 60000},
    "geo": {"address": [{"id": 1, "qs": "room1=1", "name": "Москва"}]},
    "user": {"agentAvailability": {"available": True}, "isAgent": True},
    "photoFeatureIcons": [{"link": "https://www.cian.ru/rent/flat/1/?context=Q", "type": "hasVideo"}],
}


def variant(**kw):
    import copy
    p = copy.deepcopy(BASE)
    for k, v in kw.items():
        p[k] = v
    return p


def test_noise_does_not_change_hash():
    noisy = variant(fullUrl="https://www.cian.ru/rent/flat/1/?context=BBB&mlSearchSessionGuid=2",
                    contextToken="y", humanizedTimedelta="вчера", added="2 дня назад",
                    addedTimestamp=200,
                    geo={"address": [{"id": 1, "qs": "room2=1", "name": "Москва"}]},
                    user={"agentAvailability": {"available": False}, "isAgent": True},
                    photoFeatureIcons=[{"link": "https://www.cian.ru/rent/flat/1/?context=R",
                                        "type": "hasVideo"}])
    assert payload_hash(noisy) == payload_hash(BASE)


def test_real_changes_change_hash():
    for change in (dict(bargainTerms={"price": 55000}), dict(title="Однушка"),
                   dict(user={"isAgent": False}),
                   dict(photoFeatureIcons=[])):
        assert payload_hash(variant(**change)) != payload_hash(BASE), change


def test_key_order_does_not_matter():
    assert payload_hash(dict(reversed(list(BASE.items())))) == payload_hash(BASE)


def test_restore_takes_own_bump_time():
    assert restore(BASE, 555)["addedTimestamp"] == 555
    assert "addedTimestamp" not in restore(BASE, None)
    assert BASE["addedTimestamp"] == 100        # база не портится


class FakeCursor:
    def __init__(self, last):
        self.last, self.sql, self.next_id = last, [], 1000

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "distinct on" in sql:
            self._rows = self.last
        else:
            self._rows = [(self.next_id,)]
            self.next_id += 1

    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0]


class FakeConn:
    def __init__(self, last):
        self.cur = FakeCursor(last)

    def cursor(self): return self.cur
    def commit(self): pass


def inserts(conn):
    return [p for s, p in conn.cur.sql if s.strip().startswith("insert")]


def test_unchanged_card_is_written_as_reference(monkeypatch):
    monkeypatch.setattr(db, "scrub_payload", lambda p: p)
    conn = FakeConn([("1", 77, payload_hash(BASE))])
    n = db.save_snapshots(conn, 5, 9, "2026-10-24", [variant(addedTimestamp=300)], 1)
    (row,) = inserts(conn)
    assert n == 1
    # payload пустой, ссылка на 77, время подъема свое
    assert row[7] == 77 and row[8].obj == 300


def test_changed_card_and_first_meeting_are_written_in_full(monkeypatch):
    monkeypatch.setattr(db, "scrub_payload", lambda p: p)
    conn = FakeConn([("1", 77, payload_hash(BASE))])
    db.save_snapshots(conn, 5, 9, "2026-10-24", [variant(title="Однушка")], 1)
    assert inserts(conn)[0][6].obj["title"] == "Однушка"
    conn = FakeConn([])
    db.save_snapshots(conn, 5, 9, "2026-10-24", [BASE], 1)
    assert inserts(conn)[0][6].obj == BASE


def test_repeat_within_one_page_refers_to_just_written(monkeypatch):
    """Одно объявление дважды на странице: второе ссылается на первое."""
    monkeypatch.setattr(db, "scrub_payload", lambda p: p)
    conn = FakeConn([])
    db.save_snapshots(conn, 5, 9, "2026-10-24", [BASE, variant(addedTimestamp=101)], 1)
    first, second = inserts(conn)
    assert second[7] == 1000
