from agent.actuator import MockActuator
from agent.agent import ControlBoxAgent
from agent.outbox import Outbox
from agent.statemachine import ESTOP, IDLE, LOADED, LOADING, ConveyorStateMachine
from common.protocol import Command


def make_agent(online=True):
    sent = []
    state = {"online": online}

    def pub(evt):
        if state["online"]:
            sent.append(evt)
            return True
        return False

    ag = ControlBoxAgent("TRK-01", Outbox(), MockActuator(), pub)
    return ag, sent, state


def test_state_machine_load_cycle():
    sm = ConveyorStateMachine()
    assert sm.apply("LOAD", "J1", 2).ok
    assert sm.state == LOADING
    assert sm.step() == (True, False)
    assert sm.step() == (True, True)
    assert sm.state == LOADED and sm.load_count == 2


def test_state_machine_rejects_invalid():
    sm = ConveyorStateMachine()
    assert not sm.apply("RESET").ok            # 정상 상태에서 해제 불가
    assert not sm.apply("LOAD", "J1", 0).ok    # 수량 0 거부
    sm.apply("LOAD", "J1", 3)
    assert not sm.apply("LOAD", "J2", 1).ok    # 구동 중 중복 지시 거부


def test_estop_from_any_state_and_resume_after_reset():
    sm = ConveyorStateMachine()
    sm.apply("LOAD", "J1", 3)
    sm.step()
    assert sm.apply("ESTOP").ok and sm.state == ESTOP
    assert not sm.apply("LOAD", "J1", 2).ok    # 비상정지 중 구동 불가
    assert sm.apply("RESET").ok and sm.state == LOADED  # 이미 1개 실려 있음
    assert sm.apply("LOAD", "J1", 2).ok        # 남은 수량 재개


def test_duplicate_command_runs_once():
    ag, sent, _ = make_agent()
    cmd = Command(type="LOAD", job_id="J1", pallets=2).to_json()
    ag.handle_command(cmd)
    ag.tick()
    evts = ag.handle_command(cmd)               # 서버가 ACK를 못 받아 재전송한 상황
    assert evts[0].payload.get("duplicate") is True
    assert ag.sm.done == 1                      # 다시 처음부터 시작하지 않음
    assert ag.sm.state == LOADING


def test_events_survive_disconnect_and_flush_in_order():
    ag, sent, net = make_agent(online=True)
    ag.handle_command(Command(type="LOAD", job_id="J1", pallets=3).to_json())
    for e in list(sent):                        # 브로커 PUBACK 받은 것은 삭제
        ag.outbox.ack(e.evt_id)
    net["online"] = False                       # LTE 단절
    for _ in range(3):
        ag.tick()
    assert ag.outbox.count() == 4               # PROGRESS x3 + DONE, 로컬 보관
    net["online"] = True
    before = len(sent)
    assert ag.flush() == 4
    resent = sent[before:]
    assert [e.seq for e in resent] == sorted(e.seq for e in resent)
    assert resent[-1].type == "DONE"


def test_local_estop_button_stops_motor():
    ag, sent, _ = make_agent()
    ag.handle_command(Command(type="LOAD", job_id="J1", pallets=5).to_json())
    assert ag.act.motion == "FWD"
    evt = ag.press_estop()
    assert ag.act.motion == "STOP" and ag.sm.state == ESTOP
    assert evt.payload["source"] == "local_button"
    assert ag.tick() == []                      # 정지 중에는 진행 없음


def test_bad_command_is_dropped():
    ag, sent, _ = make_agent()
    assert ag.handle_command(b"not json") == []
    assert ag.handle_command('{"type": "FLY"}') == []
    assert ag.sm.state == IDLE
