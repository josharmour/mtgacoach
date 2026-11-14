"""Produce Q4's offline fixture query output; no server or private data needed."""
import json
import sqlite3
import tempfile
from pathlib import Path

from coach_outcomes import load_outcomes


def example():
    with tempfile.TemporaryDirectory(prefix='q4-fixture-') as tmp:
        db = Path(tmp) / 'outcomes.db'
        with sqlite3.connect(db) as conn:
            conn.executescript('''
                CREATE TABLE match_bundles(owner,install_id,match_id,received_at,result,platform,
                                          autopilot_enabled,format_name,deck_name);
                CREATE TABLE subscribers(license_key,email,name);
                CREATE TABLE trials(litellm_key,email);
            ''')
            conn.executemany('INSERT INTO match_bundles VALUES (?,?,?,?,?,?,?,?,?)', [
                ('fixture','mac','game1',10,'win','darwin',1,'Ranked','Deck A'),
                ('fixture','mac','game2',11,'loss','darwin',0,'Ranked','Deck A'),
                ('fixture','mac','game3',12,'win','darwin',1,'Unranked','Deck B'),
                ('fixture','windows','game3',13,'win','windows',1,'Unranked','Deck B'),
                ('fixture','mac','game4',14,'draw','darwin',None,None,None),
            ])
        return load_outcomes(db, [('fixture', 'trial-fixture', 'Fixture User', '')], 0, 20)


if __name__ == '__main__':
    print(json.dumps(example(), indent=2))
