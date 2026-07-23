import sqlite3
import threading
import time

import arenamcp.mtgadb as mtgadb_module
from arenamcp.mtgadb import MTGADatabase


def _create_test_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE Cards (
                GrpId INTEGER PRIMARY KEY,
                TitleId INTEGER,
                Types TEXT,
                Power TEXT,
                Toughness TEXT,
                Colors TEXT,
                IsToken INTEGER,
                ExpansionCode TEXT,
                AbilityIds TEXT,
                Order_Title TEXT
            );
            CREATE TABLE Localizations_enUS (
                LocId INTEGER PRIMARY KEY,
                Loc TEXT,
                Formatted INTEGER
            );
            CREATE TABLE Abilities (
                Id INTEGER PRIMARY KEY,
                TextId INTEGER
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO Cards
                (GrpId, TitleId, Types, Power, Toughness, Colors, IsToken, ExpansionCode, AbilityIds, Order_Title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1001, 2001, "Creature", "2", "2", "G", 0, "TST", "1:3001,2:3002", "Fallback One"),
                (1002, 2002, "Instant", "", "", "U", 0, "TST", None, "Fallback Two"),
            ],
        )
        conn.executemany(
            "INSERT INTO Localizations_enUS (LocId, Loc, Formatted) VALUES (?, ?, ?)",
            [
                (2001, "Test Creature", 1),
                (2002, "Test Instant", 1),
                (3001, "Flying", 1),
                (3002, "Draw a card.", 1),
                (4001, "Counter target spell.", 1),
            ],
        )
        conn.execute("INSERT INTO Abilities (Id, TextId) VALUES (?, ?)", (9001, 4001))
        conn.commit()
    finally:
        conn.close()


def test_mtgadb_reads_cards_and_batch_queries(tmp_path):
    db_path = tmp_path / "carddb.sqlite"
    _create_test_db(db_path)

    db = MTGADatabase(db_path=db_path)
    try:
        card = db.get_card(1001)
        assert card is not None
        assert card.name == "Test Creature"
        assert card.oracle_text == "Flying\nDraw a card."

        batch = db.get_cards_batch([1001, 1002])
        assert set(batch) == {1001, 1002}
        assert batch[1002].name == "Test Instant"
        assert db.get_ability_text(9001) == "Counter target spell."
    finally:
        db.close()


def test_mtgadb_prewarm_cards_and_cache(tmp_path):
    db_path = tmp_path / "carddb.sqlite"
    _create_test_db(db_path)

    db = MTGADatabase(db_path=db_path)
    try:
        # Before pre-warming, cache should be empty
        assert 1001 not in db._card_cache
        assert 1002 not in db._card_cache

        # Pre-warm cards
        prewarmed = db.prewarm_cards([1001, 1002])
        assert len(prewarmed) == 2
        assert 1001 in db._card_cache
        assert 1002 in db._card_cache
        assert db._card_cache[1001].name == "Test Creature"
        assert db._card_cache[1002].name == "Test Instant"

        # Sequential get_card calls return cached object instantly
        card1 = db.get_card(1001)
        assert card1 is db._card_cache[1001]

        # clear_cache empties the cache
        db.clear_cache()
        assert 1001 not in db._card_cache
    finally:
        db.close()


class _GuardedConnection:
    def __init__(self, real_conn):
        object.__setattr__(self, "_real_conn", real_conn)
        object.__setattr__(self, "_owner_tid", None)
        object.__setattr__(self, "_depth", 0)
        object.__setattr__(self, "conflicts", 0)

    def __getattr__(self, name):
        return getattr(self._real_conn, name)

    def __setattr__(self, name, value):
        if name in {"_real_conn", "_owner_tid", "_depth", "conflicts"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real_conn, name, value)

    def execute(self, *args, **kwargs):
        tid = threading.get_ident()
        owner_tid = self._owner_tid
        if owner_tid not in (None, tid):
            self.conflicts += 1
            raise AssertionError("concurrent execute on shared SQLite connection")

        object.__setattr__(self, "_owner_tid", tid)
        object.__setattr__(self, "_depth", self._depth + 1)
        time.sleep(0.01)
        try:
            return self._real_conn.execute(*args, **kwargs)
        finally:
            depth = self._depth - 1
            object.__setattr__(self, "_depth", depth)
            if depth == 0:
                object.__setattr__(self, "_owner_tid", None)


def test_mtgadb_serializes_shared_connection_access(tmp_path, monkeypatch):
    db_path = tmp_path / "carddb.sqlite"
    _create_test_db(db_path)

    real_connect = sqlite3.connect
    guarded_connections = []

    def guarded_connect(*args, **kwargs):
        wrapper = _GuardedConnection(real_connect(*args, **kwargs))
        guarded_connections.append(wrapper)
        return wrapper

    monkeypatch.setattr(mtgadb_module.sqlite3, "connect", guarded_connect)

    db = MTGADatabase(db_path=db_path)
    try:
        results = []

        def worker():
            for _ in range(10):
                results.append(db.get_card(1001))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 80
        assert all(card is not None and card.name == "Test Creature" for card in results)
        assert sum(conn.conflicts for conn in guarded_connections) == 0
    finally:
        db.close()
