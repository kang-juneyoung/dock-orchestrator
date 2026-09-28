"""장비-서버 간 공통 메시지 규약.

- MQTT 토픽은 tenant(고객사) 단위로 분리한다.
- 명령(cmd)과 이벤트(evt)는 모두 고유 ID를 가진다.
  * cmd_id : 같은 명령이 두 번 와도 장비가 한 번만 실행하도록(멱등성)
  * evt_id : 통신 복구 후 재전송된 이벤트를 서버가 중복 처리하지 않도록
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# MQTT 토픽
# ---------------------------------------------------------------------------
TOPIC_ROOT = "dock"


def topic_cmd(tenant: str, truck: str) -> str:
    return f"{TOPIC_ROOT}/{tenant}/{truck}/cmd"


def topic_evt(tenant: str, truck: str) -> str:
    return f"{TOPIC_ROOT}/{tenant}/{truck}/evt"


def topic_status(tenant: str, truck: str) -> str:
    return f"{TOPIC_ROOT}/{tenant}/{truck}/status"


def parse_topic(topic: str) -> tuple[str, str, str] | None:
    """'dock/<tenant>/<truck>/<kind>' -> (tenant, truck, kind)"""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != TOPIC_ROOT:
        return None
    return parts[1], parts[2], parts[3]


# ---------------------------------------------------------------------------
# 명령 / 이벤트
# ---------------------------------------------------------------------------
CMD_LOAD = "LOAD"        # 상차: 컨베이어를 안쪽으로 구동
CMD_UNLOAD = "UNLOAD"    # 하차: 바깥쪽으로 구동
CMD_ESTOP = "ESTOP"      # 비상정지 (원격)
CMD_RESET = "RESET"      # 비상정지/고장 해제
COMMANDS = {CMD_LOAD, CMD_UNLOAD, CMD_ESTOP, CMD_RESET}

EVT_ACK = "ACK"            # 명령 수신 확인
EVT_REJECT = "REJECT"      # 현재 상태에서 수행 불가
EVT_PROGRESS = "PROGRESS"  # 팔레트 1개 적재/하역 완료
EVT_DONE = "DONE"          # 작업 완료
EVT_ESTOP = "ESTOP"        # 비상정지 발생 (현장 버튼 또는 원격)
EVT_FAULT = "FAULT"        # 장비 이상
EVT_RESET = "RESET"        # 해제 완료


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Command:
    type: str
    job_id: str | None = None
    pallets: int = 0
    cmd_id: str = field(default_factory=new_id)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "Command":
        d = json.loads(raw)
        if d.get("type") not in COMMANDS:
            raise ValueError(f"unknown command type: {d.get('type')}")
        return Command(**{k: d[k] for k in ("type", "job_id", "pallets", "cmd_id", "ts") if k in d})


@dataclass
class Event:
    type: str
    truck: str
    seq: int
    job_id: str | None = None
    cmd_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    evt_id: str = field(default_factory=new_id)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "Event":
        d = json.loads(raw)
        return Event(**d)
