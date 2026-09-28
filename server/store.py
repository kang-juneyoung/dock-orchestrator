"""영속 저장소 (SQLite). 모든 테이블에 tenant 컬럼을 두어 고객사 데이터를 분리한다.

운영 환경이라면 PostgreSQL로 바꾸는 자리다. 쿼리는 표준 SQL만 사용했다.
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    tenant TEXT, order_id TEXT, dock TEXT, pallets INTEGER, sku TEXT,
    status TEXT, created REAL, PRIMARY KEY(tenant, order_id));
CREATE TABLE IF NOT EXISTS dispatches(
    tenant TEXT, truck TEXT, dock TEXT, eta TEXT, status TEXT, created REAL,
    PRIMARY KEY(tenant, truck, dock));
CREATE TABLE IF NOT EXISTS jobs(
    job_id TEXT PRIMARY KEY, tenant TEXT, dock TEXT, truck TEXT, order_id TEXT,
    pallets INTEGER, done INTEGER DEFAULT 0, status TEXT, cmd_id TEXT, cmd_body TEXT,
    cmd_sent REAL, created REAL, updated REAL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS trucks(
    tenant TEXT, truck TEXT, state TEXT, online INTEGER, last_seen REAL,
    load_count INTEGER, pending INTEGER, PRIMARY KEY(tenant, truck));
CREATE TABLE IF NOT EXISTS events_seen(evt_id TEXT PRIMARY KEY, tenant TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS alarms(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, truck TEXT, job_id TEXT,
    kind TEXT, level TEXT, message TEXT, ts REAL, cleared REAL);
CREATE TABLE IF NOT EXISTS usage(
    tenant TEXT, month TEXT, jobs INTEGER DEFAULT 0, pallets INTEGER DEFAULT 0,
    PRIMARY KEY(tenant, month));
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, target TEXT, body TEXT, ts REAL);
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def q(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, *args) -> int:
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.rowcount

    # --- 편의 함수 --------------------------------------------------------------
    def mark_event(self, tenant: str, evt_id: str) -> bool:
        """처음 보는 이벤트면 True. 재전송된 중복이면 False."""
        try:
            self.x("INSERT INTO events_seen VALUES(?,?,?)", evt_id, tenant, time.time())
            return True
        except sqlite3.IntegrityError:
            return False

    def raise_alarm(self, tenant: str, truck: str | None, job_id: str | None,
                    kind: str, level: str, message: str) -> bool:
        """같은 종류의 미해제 경보가 있으면 새로 만들지 않는다."""
        if self.one("SELECT id FROM alarms WHERE tenant=? AND IFNULL(truck,'')=IFNULL(?,'') "
                    "AND kind=? AND cleared IS NULL", tenant, truck, kind):
            return False
        self.x("INSERT INTO alarms(tenant,truck,job_id,kind,level,message,ts) VALUES(?,?,?,?,?,?,?)",
               tenant, truck, job_id, kind, level, message, time.time())
        return True

    def clear_alarm(self, tenant: str, truck: str | None, kind: str) -> bool:
        return self.x("UPDATE alarms SET cleared=? WHERE tenant=? AND IFNULL(truck,'')=IFNULL(?,'') "
                      "AND kind=? AND cleared IS NULL", time.time(), tenant, truck, kind) > 0

    def add_usage(self, tenant: str, pallets: int) -> None:
        month = time.strftime("%Y-%m")
        self.x("INSERT INTO usage(tenant,month,jobs,pallets) VALUES(?,?,1,?) "
               "ON CONFLICT(tenant,month) DO UPDATE SET jobs=jobs+1, pallets=pallets+excluded.pallets",
               tenant, month, pallets)

    def add_report(self, tenant: str, target: str, body: dict) -> None:
        self.x("INSERT INTO reports(tenant,target,body,ts) VALUES(?,?,?,?)",
               tenant, target, json.dumps(body, ensure_ascii=False), time.time())
