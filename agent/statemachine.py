"""컨트롤박스 상태머신.

통신과 분리된 순수 로직이라 단위 테스트가 쉽다.
상태 전이 표:

    IDLE/LOADED --LOAD-->  LOADING  --(완료)--> LOADED
    LOADED    --UNLOAD-->  UNLOADING --(완료)--> IDLE
    IDLE      --UNLOAD-->  UNLOADING            (적재 상태를 모르는 차량도 하역 허용)
    (모든 상태) --ESTOP-->  ESTOP     : 모터 즉시 정지
    ESTOP/FAULT --RESET--> IDLE (적재물이 남아 있으면 LOADED)
    LOADING/UNLOADING --(센서 이상)--> FAULT
"""
from __future__ import annotations

from dataclasses import dataclass

from common.protocol import CMD_ESTOP, CMD_LOAD, CMD_RESET, CMD_UNLOAD

IDLE = "IDLE"
LOADING = "LOADING"
LOADED = "LOADED"
UNLOADING = "UNLOADING"
ESTOP = "ESTOP"
FAULT = "FAULT"

BUSY = {LOADING, UNLOADING}

# 명령별로 허용되는 출발 상태
_ALLOWED = {
    CMD_LOAD: {IDLE, LOADED},   # 부분 적재 차량에 추가 적재 / 비상정지 후 재개
    CMD_UNLOAD: {IDLE, LOADED},
    CMD_ESTOP: {IDLE, LOADING, LOADED, UNLOADING, FAULT, ESTOP},
    CMD_RESET: {ESTOP, FAULT},
}


@dataclass
class Transition:
    ok: bool
    new_state: str
    reason: str = ""


class ConveyorStateMachine:
    def __init__(self) -> None:
        self.state = IDLE
        self.job_id: str | None = None
        self.target = 0      # 이번 작업의 목표 팔레트 수
        self.done = 0        # 처리한 팔레트 수
        self.load_count = 0  # 현재 적재함 안의 팔레트 수

    def can(self, cmd: str) -> bool:
        return self.state in _ALLOWED.get(cmd, set())

    def apply(self, cmd: str, job_id: str | None = None, pallets: int = 0) -> Transition:
        if not self.can(cmd):
            return Transition(False, self.state, f"{cmd} not allowed in {self.state}")
        if cmd == CMD_LOAD:
            if pallets <= 0:
                return Transition(False, self.state, "pallets must be > 0")
            self._start(LOADING, job_id, pallets)
        elif cmd == CMD_UNLOAD:
            self._start(UNLOADING, job_id, pallets or self.load_count)
        elif cmd == CMD_ESTOP:
            self.state = ESTOP
        elif cmd == CMD_RESET:
            self.state = LOADED if self.load_count > 0 else IDLE
            self.job_id, self.target, self.done = None, 0, 0
        return Transition(True, self.state)

    def _start(self, state: str, job_id: str | None, target: int) -> None:
        self.state, self.job_id, self.target, self.done = state, job_id, target, 0

    def step(self) -> tuple[bool, bool]:
        """구동 중 팔레트 1개 처리. (진행했는지, 작업이 끝났는지)"""
        if self.state not in BUSY:
            return False, False
        self.done += 1
        if self.state == LOADING:
            self.load_count += 1
        else:
            self.load_count = max(0, self.load_count - 1)
        if self.done >= self.target:
            self.state = LOADED if self.state == LOADING else IDLE
            return True, True
        return True, False

    def fault(self, reason: str) -> None:
        self.state = FAULT

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "job_id": self.job_id,
            "target": self.target,
            "done": self.done,
            "load_count": self.load_count,
        }
