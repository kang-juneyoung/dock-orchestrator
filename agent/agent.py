"""컨트롤박스 에이전트 (트럭 적재함 쪽 장비 소프트웨어).

역할
- 클라우드에서 MQTT로 명령(LOAD/UNLOAD/ESTOP/RESET)을 받는다
- 상태머신에 따라 구동부를 움직이고 진행 이벤트를 올린다
- 통신이 끊겨도 이벤트를 로컬에 쌓아두었다가 복구되면 순서대로 재전송한다
- 같은 명령이 두 번 와도 한 번만 실행한다 (cmd_id 기록)

실행 예)
    python -m agent.agent --tenant acme --truck TRK-01
    python -m agent.agent --tenant acme --truck TRK-01 --actuator gpio   # 라즈베리파이

시험용 신호 (Linux/macOS)
    kill -USR1 <pid>   통신 끊기/복구 토글 (LTE 단절 재현)
    kill -USR2 <pid>   현장 비상정지 버튼 누름 재현
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from typing import Callable

from agent.actuator import Actuator, make_actuator
from agent.outbox import Outbox
from agent.statemachine import LOADING, UNLOADING, ConveyorStateMachine
from common.protocol import (
    CMD_ESTOP,
    CMD_LOAD,
    CMD_RESET,
    CMD_UNLOAD,
    EVT_ACK,
    EVT_DONE,
    EVT_ESTOP,
    EVT_PROGRESS,
    EVT_REJECT,
    EVT_RESET,
    Command,
    Event,
    topic_cmd,
    topic_evt,
    topic_status,
)

log = logging.getLogger("agent")


class ControlBoxAgent:
    """통신 계층과 분리된 에이전트 본체. publish 함수만 주입받는다."""

    def __init__(
        self,
        truck: str,
        outbox: Outbox,
        actuator: Actuator,
        publish: Callable[[Event], bool],
    ) -> None:
        self.truck = truck
        self.outbox = outbox
        self.act = actuator
        self.sm = ConveyorStateMachine()
        self._publish = publish
        self._lock = threading.RLock()

    # --- 명령 처리 -------------------------------------------------------------
    def handle_command(self, raw: str | bytes) -> list[Event]:
        try:
            cmd = Command.from_json(raw)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            log.warning("bad command dropped: %s", e)
            return []

        with self._lock:
            prev = self.outbox.seen(cmd.cmd_id)
            if prev is not None:
                # 이미 처리한 명령: 구동하지 않고 이전 결과만 다시 알려준다
                log.info("duplicate cmd %s ignored (was %s)", cmd.cmd_id[:8], prev)
                return [self.emit(EVT_ACK if prev == "ok" else EVT_REJECT, cmd.job_id, cmd.cmd_id,
                                  {"duplicate": True, "state": self.sm.state})]

            tr = self.sm.apply(cmd.type, cmd.job_id, cmd.pallets)
            self.outbox.remember(cmd.cmd_id, "ok" if tr.ok else "reject")
            if not tr.ok:
                return [self.emit(EVT_REJECT, cmd.job_id, cmd.cmd_id, {"reason": tr.reason})]

            events = [self.emit(EVT_ACK, cmd.job_id, cmd.cmd_id, {"state": tr.new_state})]
            if cmd.type == CMD_LOAD:
                self.act.forward()
            elif cmd.type == CMD_UNLOAD:
                self.act.reverse()
            elif cmd.type == CMD_ESTOP:
                self.act.stop()
                events.append(self.emit(EVT_ESTOP, self.sm.job_id, cmd.cmd_id, {"source": "remote"}))
            elif cmd.type == CMD_RESET:
                self.act.stop()
                events.append(self.emit(EVT_RESET, None, cmd.cmd_id, {"state": self.sm.state}))
            return events

    def press_estop(self) -> Event | None:
        """현장 비상정지 버튼. 통신 여부와 무관하게 즉시 모터를 멈춘다."""
        with self._lock:
            self.act.stop()
            tr = self.sm.apply(CMD_ESTOP)
            if tr.ok:
                return self.emit(EVT_ESTOP, self.sm.job_id, None, {"source": "local_button"})
        return None

    # --- 구동 진행 -------------------------------------------------------------
    def tick(self) -> list[Event]:
        """주기적으로 호출. 구동 중이면 팔레트 1개를 처리한다."""
        with self._lock:
            if self.sm.state not in (LOADING, UNLOADING):
                return []
            job = self.sm.job_id
            _, finished = self.sm.step()
            snap = self.sm.snapshot()
            events = [self.emit(EVT_PROGRESS, job, None,
                                {"done": snap["done"], "target": snap["target"],
                                 "load_count": snap["load_count"]})]
            if finished:
                self.act.stop()
                events.append(self.emit(EVT_DONE, job, None,
                                        {"pallets": snap["done"], "load_count": snap["load_count"],
                                         "state": snap["state"]}))
            return events

    # --- 이벤트 발행 -----------------------------------------------------------
    def emit(self, typ: str, job_id: str | None, cmd_id: str | None, payload: dict) -> Event:
        evt = Event(type=typ, truck=self.truck, seq=self.outbox.next_seq(),
                    job_id=job_id, cmd_id=cmd_id, payload=payload)
        self.outbox.put(evt)          # 1) 먼저 저장
        self._publish(evt)            # 2) 보낼 수 있으면 보낸다 (실패해도 남아 있음)
        log.info("evt %-8s seq=%d job=%s %s", typ, evt.seq, (job_id or "-")[:8], payload)
        return evt

    def flush(self) -> int:
        """재연결 시 미전송 이벤트를 순서대로 재전송"""
        pending = self.outbox.pending()
        for evt in pending:
            self._publish(evt)
        return len(pending)

    def status(self) -> dict:
        with self._lock:
            return {**self.sm.snapshot(), "truck": self.truck,
                    "pending_events": self.outbox.count(), "ts": time.time()}


class MqttLink:
    """paho-mqtt 연결 관리. 끊김 시 자동 재접속, 접속 시 아웃박스 flush."""

    def __init__(self, tenant: str, truck: str, host: str, port: int) -> None:
        import paho.mqtt.client as mqtt

        self.tenant, self.truck = tenant, truck
        self.host, self.port = host, port
        self.connected = False
        self.cut = False  # 시험용: 통신 단절 상태
        self._mid_to_evt: dict[int, str] = {}
        self.agent: ControlBoxAgent | None = None

        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"agent-{tenant}-{truck}",
                             clean_session=False)
        self.c.will_set(topic_status(tenant, truck), json.dumps({"online": False, "truck": truck}),
                        qos=1, retain=True)
        self.c.reconnect_delay_set(min_delay=1, max_delay=10)
        self.c.on_connect = self._on_connect
        self.c.on_disconnect = self._on_disconnect
        self.c.on_message = self._on_message
        self.c.on_publish = self._on_publish

    def start(self) -> None:
        self.c.connect_async(self.host, self.port, keepalive=5)
        self.c.loop_start()

    def publish_event(self, evt: Event) -> bool:
        if not self.connected or self.cut:
            return False
        info = self.c.publish(topic_evt(self.tenant, self.truck), evt.to_json(), qos=1)
        self._mid_to_evt[info.mid] = evt.evt_id
        return True

    def publish_status(self, status: dict) -> None:
        if self.connected and not self.cut:
            self.c.publish(topic_status(self.tenant, self.truck),
                           json.dumps({"online": True, **status}), qos=0, retain=True)

    def toggle_cut(self) -> None:
        self.cut = not self.cut
        if self.cut:
            log.warning("=== 통신 단절 (시험) ===")
            self.c.disconnect()
        else:
            log.warning("=== 통신 복구 (시험) ===")
            # disconnect() 후에는 네트워크 루프 스레드가 끝나므로 다시 시작한다
            self.c.loop_stop()
            self.c.connect_async(self.host, self.port, keepalive=5)
            self.c.loop_start()

    # --- paho 콜백 -------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("connect failed: %s", reason_code)
            return
        self.connected = True
        self._mid_to_evt.clear()
        client.subscribe(topic_cmd(self.tenant, self.truck), qos=1)
        n = self.agent.flush() if self.agent else 0
        log.info("MQTT connected %s:%d, resent %d pending events", self.host, self.port, n)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        log.warning("MQTT disconnected (%s) - 이벤트는 로컬에 저장됩니다", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        if self.agent:
            self.agent.handle_command(msg.payload)

    def _on_publish(self, client, userdata, mid, reason_code, properties) -> None:
        evt_id = self._mid_to_evt.pop(mid, None)
        if evt_id and self.agent:
            self.agent.outbox.ack(evt_id)


def main() -> None:
    p = argparse.ArgumentParser(description="dock controlbox agent")
    p.add_argument("--tenant", default="acme")
    p.add_argument("--truck", default="TRK-01")
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--actuator", choices=["mock", "gpio"], default="mock")
    p.add_argument("--db", default=None, help="아웃박스 SQLite 경로 (기본: agent_<truck>.db)")
    p.add_argument("--step", type=float, default=1.0, help="팔레트 1개 처리 시간(초)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    link = MqttLink(args.tenant, args.truck, args.broker, args.port)
    agent = ControlBoxAgent(args.truck, Outbox(args.db or f"agent_{args.truck}.db"),
                            make_actuator(args.actuator), link.publish_event)
    link.agent = agent
    agent.act.on_estop_button(agent.press_estop)

    signal.signal(signal.SIGUSR1, lambda *_: link.toggle_cut())
    signal.signal(signal.SIGUSR2, lambda *_: agent.press_estop())
    link.start()

    last_status = 0.0
    while True:
        agent.tick()
        now = time.time()
        if now - last_status >= 1.0:
            link.publish_status(agent.status())
            last_status = now
        time.sleep(args.step)


if __name__ == "__main__":
    main()
