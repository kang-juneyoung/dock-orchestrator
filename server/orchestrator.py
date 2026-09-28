"""도크 오케스트레이터 코어.

흐름
  WMS 출고 오더(어느 도크로 무엇을)  ─┐
  TMS 배차(어느 트럭이 어느 도크로)  ─┴─> 같은 도크에서 만나면 '도크 작업(job)' 생성 [WAITING_GOODS]
  로봇(AMR)이 짐을 도크에 내려놓음  ───> 트럭 컨트롤박스에 LOAD 명령           [DISPATCHED]
  컨트롤박스 ACK                    ───> 적재 중                                [LOADING]
  컨트롤박스 DONE                   ───> 완료, WMS/TMS에 결과 보고, 사용량 집계 [COMPLETED]
  비상정지                          ───> 중단, 경보                             [HALTED]

통신 계층(MQTT)과 시간(now)을 주입받으므로 브로커 없이 단위 테스트가 가능하다.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from common.protocol import (
    CMD_ESTOP,
    CMD_LOAD,
    CMD_RESET,
    EVT_ACK,
    EVT_DONE,
    EVT_ESTOP,
    EVT_FAULT,
    EVT_PROGRESS,
    EVT_REJECT,
    EVT_RESET,
    Command,
    Event,
    new_id,
)
from server.adapters.base import NormalizedDispatch, NormalizedOrder
from server.store import Store

WAITING_GOODS = "WAITING_GOODS"
DISPATCHED = "DISPATCHED"
LOADING = "LOADING"
COMPLETED = "COMPLETED"
HALTED = "HALTED"
FAILED = "FAILED"
ACTIVE = (WAITING_GOODS, DISPATCHED, LOADING, HALTED)

SendFn = Callable[[str, str, Command], None]


class Orchestrator:
    def __init__(self, store: Store, send: SendFn, *, heartbeat_timeout: float = 5,
                 ack_timeout: float = 4, stall_timeout: float = 15,
                 clock: Callable[[], float] = time.time) -> None:
        self.s = store
        self.send = send
        self.hb_timeout = heartbeat_timeout
        self.ack_timeout = ack_timeout
        self.stall_timeout = stall_timeout
        self.now = clock

    # ------------------------------------------------------------------ 입력: 상위 시스템
    def on_order(self, tenant: str, o: NormalizedOrder) -> list[dict]:
        self.s.x("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?)",
                 tenant, o.order_id, o.dock, o.pallets, o.sku_summary, "WAITING", self.now())
        return self._match(tenant, o.dock)

    def on_dispatch(self, tenant: str, d: NormalizedDispatch) -> list[dict]:
        self.s.x("INSERT OR REPLACE INTO dispatches VALUES(?,?,?,?,?,?)",
                 tenant, d.truck, d.dock, d.eta, "WAITING", self.now())
        return self._match(tenant, d.dock)

    def _match(self, tenant: str, dock: str) -> list[dict]:
        created = []
        while True:
            order = self.s.one("SELECT * FROM orders WHERE tenant=? AND dock=? AND status='WAITING' "
                               "ORDER BY created LIMIT 1", tenant, dock)
            disp = self.s.one("SELECT * FROM dispatches WHERE tenant=? AND dock=? AND status='WAITING' "
                              "ORDER BY created LIMIT 1", tenant, dock)
            if not order or not disp:
                return created
            busy = self.s.one("SELECT job_id FROM jobs WHERE tenant=? AND truck=? AND status IN "
                              "(?,?,?,?)", tenant, disp["truck"], *ACTIVE)
            if busy:
                return created
            job_id = "J" + new_id()[:10]
            now = self.now()
            self.s.x("INSERT INTO jobs(job_id,tenant,dock,truck,order_id,pallets,status,created,updated) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", job_id, tenant, dock, disp["truck"],
                     order["order_id"], order["pallets"], WAITING_GOODS, now, now)
            self.s.x("UPDATE orders SET status='MATCHED' WHERE tenant=? AND order_id=?", tenant, order["order_id"])
            self.s.x("UPDATE dispatches SET status='MATCHED' WHERE tenant=? AND truck=? AND dock=?",
                     tenant, disp["truck"], dock)
            created.append(self.job(job_id))

    # ------------------------------------------------------------------ 입력: 로봇
    def on_amr_arrival(self, tenant: str, dock: str, order_id: str | None = None) -> dict | None:
        """AMR이 도크에 짐을 내려놓았다 -> 트럭에 적재 명령"""
        sql = "SELECT * FROM jobs WHERE tenant=? AND dock=? AND status=?"
        args: list = [tenant, dock, WAITING_GOODS]
        if order_id:
            sql += " AND order_id=?"
            args.append(order_id)
        job = self.s.one(sql + " ORDER BY created LIMIT 1", *args)
        if not job:
            return None
        self._dispatch(job, CMD_LOAD, job["pallets"] - job["done"])
        return self.job(job["job_id"])

    def _dispatch(self, job: dict, typ: str, pallets: int) -> None:
        cmd = Command(type=typ, job_id=job["job_id"], pallets=pallets)
        self.s.x("UPDATE jobs SET status=?, cmd_id=?, cmd_body=?, cmd_sent=?, updated=? WHERE job_id=?",
                 DISPATCHED, cmd.cmd_id, cmd.to_json(), self.now(), self.now(), job["job_id"])
        self.send(job["tenant"], job["truck"], cmd)

    # ------------------------------------------------------------------ 입력: 운영자
    def remote_estop(self, tenant: str, truck: str) -> None:
        self.send(tenant, truck, Command(type=CMD_ESTOP))

    def remote_reset(self, tenant: str, truck: str) -> None:
        self.send(tenant, truck, Command(type=CMD_RESET))

    def resume(self, tenant: str, job_id: str) -> dict | None:
        """비상정지로 멈춘 작업을 남은 수량만큼 다시 지시"""
        job = self.s.one("SELECT * FROM jobs WHERE tenant=? AND job_id=? AND status=?", tenant, job_id, HALTED)
        if not job:
            return None
        self._dispatch(job, CMD_LOAD, job["pallets"] - job["done"])
        return self.job(job_id)

    # ------------------------------------------------------------------ 입력: 장비
    def on_status(self, tenant: str, truck: str, st: dict) -> bool:
        if not st.get("online", True):
            return False  # LWT(비정상 종료) - 하트비트 만료로 처리
        prev = self.s.one("SELECT online FROM trucks WHERE tenant=? AND truck=?", tenant, truck)
        self.s.x("INSERT OR REPLACE INTO trucks VALUES(?,?,?,?,?,?,?)", tenant, truck,
                 st.get("state"), 1, self.now(), st.get("load_count", 0), st.get("pending_events", 0))
        if prev is not None and not prev["online"]:
            self.s.clear_alarm(tenant, truck, "COMM_LOST")
        return True

    def on_event(self, tenant: str, evt: Event) -> bool:
        if not self.s.mark_event(tenant, evt.evt_id):
            return False  # 통신 복구 후 재전송된 중복 이벤트
        job = self.s.one("SELECT * FROM jobs WHERE job_id=? AND tenant=?", evt.job_id, tenant) if evt.job_id else None
        now = self.now()

        if evt.type == EVT_ACK and job and evt.cmd_id == job["cmd_id"] and job["status"] == DISPATCHED:
            self.s.x("UPDATE jobs SET status=?, updated=? WHERE job_id=?", LOADING, now, job["job_id"])
        elif evt.type == EVT_REJECT and job and evt.cmd_id == job["cmd_id"]:
            self.s.x("UPDATE jobs SET status=?, note=?, updated=? WHERE job_id=?", FAILED,
                     evt.payload.get("reason", ""), now, job["job_id"])
            self.s.raise_alarm(tenant, evt.truck, job["job_id"], "REJECTED", "warn",
                               f"{evt.truck} 명령 거부: {evt.payload.get('reason')}")
        elif evt.type == EVT_PROGRESS and job and job["status"] in (DISPATCHED, LOADING):
            done = job["pallets"] - job_remaining(job) + evt.payload.get("done", 0)
            self.s.x("UPDATE jobs SET status=?, done=?, updated=? WHERE job_id=?",
                     LOADING, min(done, job["pallets"]), now, job["job_id"])
            self.s.clear_alarm(tenant, evt.truck, "STALL")
        elif evt.type == EVT_DONE and job and job["status"] in (DISPATCHED, LOADING):
            self.s.x("UPDATE jobs SET status=?, done=pallets, updated=? WHERE job_id=?",
                     COMPLETED, now, job["job_id"])
            self.s.add_usage(tenant, job["pallets"])
            report = {"order_id": job["order_id"], "truck": job["truck"], "dock": job["dock"],
                      "pallets": job["pallets"], "completed_at": now}
            self.s.add_report(tenant, "WMS", {"event": "OUTBOUND_LOADED", **report})
            self.s.add_report(tenant, "TMS", {"event": "READY_TO_DEPART", **report})
            self.s.clear_alarm(tenant, evt.truck, "STALL")
        elif evt.type == EVT_ESTOP:
            src = evt.payload.get("source", "")
            self.s.raise_alarm(tenant, evt.truck, evt.job_id, "ESTOP", "critical",
                               f"{evt.truck} 비상정지 ({'현장 버튼' if src == 'local_button' else '원격'})")
            if job and job["status"] in (DISPATCHED, LOADING):
                self.s.x("UPDATE jobs SET status=?, updated=? WHERE job_id=?", HALTED, now, job["job_id"])
        elif evt.type == EVT_RESET:
            self.s.clear_alarm(tenant, evt.truck, "ESTOP")
            self.s.clear_alarm(tenant, evt.truck, "FAULT")
        elif evt.type == EVT_FAULT:
            self.s.raise_alarm(tenant, evt.truck, evt.job_id, "FAULT", "critical",
                               f"{evt.truck} 장비 이상: {evt.payload}")
        return True

    # ------------------------------------------------------------------ 주기 점검
    def sweep(self) -> bool:
        """하트비트 만료, ACK 미수신 재전송, 작업 정체를 점검한다."""
        now, changed = self.now(), False
        for t in self.s.q("SELECT * FROM trucks WHERE online=1 AND last_seen < ?", now - self.hb_timeout):
            self.s.x("UPDATE trucks SET online=0 WHERE tenant=? AND truck=?", t["tenant"], t["truck"])
            self.s.raise_alarm(t["tenant"], t["truck"], None, "COMM_LOST", "critical",
                               f"{t['truck']} 통신 두절 ({self.hb_timeout:.0f}초 이상 하트비트 없음)")
            changed = True
        for j in self.s.q("SELECT j.* FROM jobs j JOIN trucks t ON t.tenant=j.tenant AND t.truck=j.truck "
                          "WHERE j.status=? AND j.cmd_sent < ? AND t.online=1", DISPATCHED, now - self.ack_timeout):
            # 같은 cmd_id로 재전송 -> 장비가 이미 받았다면 중복으로 무시한다(멱등)
            self.send(j["tenant"], j["truck"], Command.from_json(j["cmd_body"]))
            self.s.x("UPDATE jobs SET cmd_sent=?, note='명령 재전송' WHERE job_id=?", now, j["job_id"])
            changed = True
        for j in self.s.q("SELECT * FROM jobs WHERE status=? AND updated < ?", LOADING, now - self.stall_timeout):
            changed |= self.s.raise_alarm(j["tenant"], j["truck"], j["job_id"], "STALL", "warn",
                                          f"{j['truck']} 적재 진행 없음 ({self.stall_timeout:.0f}초)")
        return changed

    # ------------------------------------------------------------------ 조회
    def job(self, job_id: str) -> dict | None:
        j = self.s.one("SELECT job_id,tenant,dock,truck,order_id,pallets,done,status,note,created,updated "
                       "FROM jobs WHERE job_id=?", job_id)
        return j

    def snapshot(self, tenant: str) -> dict:
        return {
            "tenant": tenant,
            "ts": self.now(),
            "jobs": self.s.q("SELECT job_id,dock,truck,order_id,pallets,done,status,note,updated FROM jobs "
                             "WHERE tenant=? ORDER BY created DESC LIMIT 30", tenant),
            "trucks": self.s.q("SELECT truck,state,online,last_seen,load_count,pending FROM trucks "
                               "WHERE tenant=? ORDER BY truck", tenant),
            "alarms": self.s.q("SELECT id,truck,kind,level,message,ts,cleared FROM alarms WHERE tenant=? "
                               "ORDER BY id DESC LIMIT 20", tenant),
            "orders": self.s.q("SELECT order_id,dock,pallets,status FROM orders WHERE tenant=? "
                               "AND status='WAITING'", tenant),
            "dispatches": self.s.q("SELECT truck,dock,eta,status FROM dispatches WHERE tenant=? "
                                   "AND status='WAITING'", tenant),
            "reports": [dict(r, body=json.loads(r["body"])) for r in
                        self.s.q("SELECT target,body,ts FROM reports WHERE tenant=? ORDER BY id DESC LIMIT 10",
                                 tenant)],
        }

    def usage(self, tenant: str) -> list[dict]:
        return self.s.q("SELECT month,jobs,pallets FROM usage WHERE tenant=? ORDER BY month", tenant)


def job_remaining(job: dict) -> int:
    """현재 지시(cmd)가 시작될 때 남아 있던 수량. 재개(resume) 시 진행률 계산에 사용."""
    try:
        return json.loads(job["cmd_body"])["pallets"]
    except (TypeError, KeyError, json.JSONDecodeError):
        return job["pallets"]
