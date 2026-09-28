"""Store-and-forward 아웃박스.

LTE가 끊겨도 이벤트를 잃지 않기 위한 장치 측 저장소.
1) 이벤트를 만들면 먼저 SQLite에 기록한다 (전원이 나가도 남음)
2) 브로커가 수신 확인(PUBACK)을 주면 그때 삭제한다
3) 재연결되면 남은 이벤트를 seq 순서대로 다시 보낸다
서버는 evt_id로 중복을 걸러내므로, 두 번 보내져도 안전하다 (at-least-once).

처리한 명령 ID도 여기 기록해서 같은 명령이 다시 와도 두 번 구동하지 않는다.
"""
from __future__ import annotations

import sqlite3
import threading

from common.protocol import Event


class Outbox:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS outbox(
                    seq INTEGER PRIMARY KEY, evt_id TEXT UNIQUE, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seen_cmd(
                    cmd_id TEXT PRIMARY KEY, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
                """
            )
            self._db.commit()

    # --- 이벤트 --------------------------------------------------------------
    def next_seq(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k='seq'").fetchone()
            seq = int(row[0]) + 1 if row else 1
            self._db.execute("INSERT OR REPLACE INTO meta VALUES('seq', ?)", (str(seq),))
            self._db.commit()
            return seq

    def put(self, evt: Event) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO outbox(seq, evt_id, body) VALUES(?,?,?)",
                (evt.seq, evt.evt_id, evt.to_json()),
            )
            self._db.commit()

    def pending(self) -> list[Event]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM outbox ORDER BY seq").fetchall()
        return [Event.from_json(r[0]) for r in rows]

    def ack(self, evt_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM outbox WHERE evt_id=?", (evt_id,))
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # --- 명령 멱등성 ----------------------------------------------------------
    def seen(self, cmd_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT result FROM seen_cmd WHERE cmd_id=?", (cmd_id,)).fetchone()
        return row[0] if row else None

    def remember(self, cmd_id: str, result: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO seen_cmd VALUES(?,?)", (cmd_id, result))
            self._db.commit()
