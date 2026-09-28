import pytest

from common.protocol import Event
from server.adapters import formats  # noqa: F401
from server.adapters.base import TMS_ADAPTERS, WMS_ADAPTERS, AdapterError, normalize_dock
from server.orchestrator import COMPLETED, DISPATCHED, HALTED, LOADING, WAITING_GOODS, Orchestrator
from server.store import Store


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    sent = []
    clock = Clock()
    orch = Orchestrator(Store(), lambda ten, trk, cmd: sent.append((ten, trk, cmd)),
                        heartbeat_timeout=5, ack_timeout=4, stall_timeout=15, clock=clock)
    return orch, sent, clock


def order(pallets=3):
    return WMS_ADAPTERS["json_v1"]({"outboundOrder": {"orderNo": "SO-1", "dockDoor": "D-05",
                                                      "lines": [{"sku": "A", "palletQty": pallets}]}})


def dispatch():
    return TMS_ADAPTERS["csv_v1"]("truck_no,dock,eta\nTRK-01,5,09:00")[0]


def evt(typ, seq, job=None, cmd=None, **payload):
    return Event(type=typ, truck="TRK-01", seq=seq, job_id=job, cmd_id=cmd, payload=payload)


# --- 어댑터 -------------------------------------------------------------------
def test_dock_normalization():
    assert normalize_dock("D-05") == normalize_dock("5") == normalize_dock("Gate 05") == "D05"
    with pytest.raises(AdapterError):
        normalize_dock("north")


def test_two_wms_formats_normalize_to_same_model():
    a = WMS_ADAPTERS["json_v1"]({"outboundOrder": {"orderNo": "1", "dockDoor": "D-03",
                                                   "lines": [{"sku": "A", "palletQty": 4}]}})
    b = WMS_ADAPTERS["json_v2"]({"order_id": "2", "gate": "Gate 3",
                                 "items": [{"code": "X", "qty": 80, "unit": "BOX"},
                                           {"code": "Y", "qty": 2, "unit": "PLT"}]})
    assert (a.dock, a.pallets) == ("D03", 4)
    assert (b.dock, b.pallets) == ("D03", 4)    # 80박스 = 2팔레트 + 2팔레트


def test_bad_payload_raises_adapter_error():
    with pytest.raises(AdapterError):
        WMS_ADAPTERS["json_v1"]({"wrong": 1})
    with pytest.raises(AdapterError):
        TMS_ADAPTERS["csv_v1"]("a,b\n1,2")


# --- 작업 흐름 ------------------------------------------------------------------
def test_full_cycle(env):
    orch, sent, _ = env
    assert orch.on_order("acme", order(3)) == []                # 트럭 없음 -> 대기
    job = orch.on_dispatch("acme", dispatch())[0]
    assert job["status"] == WAITING_GOODS
    orch.on_amr_arrival("acme", "D05")
    _, truck, cmd = sent[-1]
    assert (truck, cmd.type, cmd.pallets) == ("TRK-01", "LOAD", 3)
    assert orch.job(job["job_id"])["status"] == DISPATCHED

    jid = job["job_id"]
    orch.on_event("acme", evt("ACK", 1, jid, cmd.cmd_id))
    assert orch.job(jid)["status"] == LOADING
    for i in range(1, 4):
        orch.on_event("acme", evt("PROGRESS", 1 + i, jid, done=i, target=3))
    orch.on_event("acme", evt("DONE", 5, jid, pallets=3))
    j = orch.job(jid)
    assert (j["status"], j["done"]) == (COMPLETED, 3)
    snap = orch.snapshot("acme")
    assert {r["target"] for r in snap["reports"]} == {"WMS", "TMS"}
    assert orch.usage("acme")[0]["pallets"] == 3


def test_duplicate_event_is_ignored(env):
    orch, sent, _ = env
    orch.on_order("acme", order(2))
    jid = orch.on_dispatch("acme", dispatch())[0]["job_id"]
    orch.on_amr_arrival("acme", "D05")
    done = evt("DONE", 9, jid, pallets=2)
    orch.on_event("acme", evt("ACK", 1, jid, sent[-1][2].cmd_id))
    assert orch.on_event("acme", done) is True
    assert orch.on_event("acme", done) is False                 # 재전송된 같은 evt_id
    assert orch.usage("acme")[0]["jobs"] == 1                   # 과금 이중 집계 없음


def test_tenants_are_isolated(env):
    orch, _, _ = env
    orch.on_order("acme", order(2))
    orch.on_dispatch("hanbit", dispatch())                      # 다른 고객사의 같은 도크
    assert orch.snapshot("acme")["jobs"] == []
    assert orch.snapshot("hanbit")["jobs"] == []


def test_truck_busy_blocks_second_job(env):
    orch, _, _ = env
    orch.on_order("acme", order(2))
    orch.on_dispatch("acme", dispatch())
    o2 = WMS_ADAPTERS["json_v1"]({"outboundOrder": {"orderNo": "SO-2", "dockDoor": "D-05",
                                                    "lines": [{"sku": "B", "palletQty": 1}]}})
    orch.on_order("acme", o2)
    assert orch.on_dispatch("acme", dispatch()) == []           # 같은 트럭이 작업 중


# --- 이상 상황 ------------------------------------------------------------------
def test_ack_timeout_resends_same_command(env):
    orch, sent, clock = env
    orch.on_status("acme", "TRK-01", {"state": "IDLE"})
    orch.on_order("acme", order(2))
    orch.on_dispatch("acme", dispatch())
    orch.on_amr_arrival("acme", "D05")
    first = sent[-1][2]
    clock.t += 3
    orch.on_status("acme", "TRK-01", {"state": "IDLE"})
    clock.t += 2                                                # ACK 없이 5초
    orch.sweep()
    assert sent[-1][2].cmd_id == first.cmd_id                   # 같은 ID로 재전송


def test_heartbeat_loss_raises_and_recovery_clears(env):
    orch, _, clock = env
    orch.on_status("acme", "TRK-01", {"state": "IDLE"})
    clock.t += 6
    assert orch.sweep()
    alarms = orch.snapshot("acme")["alarms"]
    assert alarms[0]["kind"] == "COMM_LOST" and alarms[0]["cleared"] is None
    orch.sweep()
    assert len(orch.snapshot("acme")["alarms"]) == 1            # 같은 경보 중복 생성 없음
    orch.on_status("acme", "TRK-01", {"state": "IDLE"})
    assert orch.snapshot("acme")["alarms"][0]["cleared"] is not None


def test_estop_halts_job_and_resume_sends_remaining(env):
    orch, sent, _ = env
    orch.on_order("acme", order(5))
    jid = orch.on_dispatch("acme", dispatch())[0]["job_id"]
    orch.on_amr_arrival("acme", "D05")
    orch.on_event("acme", evt("ACK", 1, jid, sent[-1][2].cmd_id))
    orch.on_event("acme", evt("PROGRESS", 2, jid, done=2))
    orch.on_event("acme", evt("ESTOP", 3, jid, source="local_button"))
    assert orch.job(jid)["status"] == HALTED
    assert orch.snapshot("acme")["alarms"][0]["level"] == "critical"
    orch.on_event("acme", evt("RESET", 4))
    orch.resume("acme", jid)
    cmd = sent[-1][2]
    assert (cmd.type, cmd.pallets) == ("LOAD", 3)               # 남은 3개만 재지시
    orch.on_event("acme", evt("ACK", 5, jid, cmd.cmd_id))
    orch.on_event("acme", evt("PROGRESS", 6, jid, done=1))
    assert orch.job(jid)["done"] == 3                           # 2 + 1


def test_stall_alarm(env):
    orch, sent, clock = env
    orch.on_order("acme", order(2))
    jid = orch.on_dispatch("acme", dispatch())[0]["job_id"]
    orch.on_amr_arrival("acme", "D05")
    orch.on_event("acme", evt("ACK", 1, jid, sent[-1][2].cmd_id))
    clock.t += 16
    orch.sweep()
    assert any(a["kind"] == "STALL" for a in orch.snapshot("acme")["alarms"])
