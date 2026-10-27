from arenamcp.rules_db import RulesDB


def test_old_toxic_cache_is_replaced_on_next_connection(tmp_path):
    path = tmp_path / "rules.db"
    old = RulesDB(path)
    conn = old._ensure_db()
    conn.execute(
        "UPDATE rules SET number='702.125a', text='Toxic is a triggered ability.' WHERE section='Toxic'"
    )
    conn.commit()
    conn.close()
    refreshed = RulesDB(path)._ensure_db()
    row = refreshed.execute("SELECT number, text FROM rules WHERE section='Toxic'").fetchone()
    assert row["number"] == "702.164a"
    assert "static, not triggered" in row["text"]
    assert "total toxic value" in row["text"]
    refreshed.close()


def test_unchanged_corpus_does_not_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "rules.db"
    RulesDB(path)._ensure_db().close()
    db = RulesDB(path)

    def unexpected():
        raise AssertionError("unchanged rules should reuse the cache")

    monkeypatch.setattr(db, "_build_db", unexpected)
    assert db.query("toxic poison")
    db._conn.close()
