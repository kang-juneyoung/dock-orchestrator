"""컨베이어 구동부 추상화.

- MockActuator : PC 시뮬레이션용. 로그만 남긴다.
- GpioActuator : 라즈베리파이 등 실물 보드. GPIO로 릴레이/LED를 구동하고
                 비상정지 버튼 입력을 받는다. (gpiozero 사용)

실제 제품이라면 모터 드라이버, 리밋 스위치, 적재 센서가 붙는 자리다.
포트폴리오에서는 LED 2개(정/역방향)와 푸시버튼 1개로 대신한다.

※ GpioActuator는 실보드에서 핀 번호를 맞춰 확인해야 한다.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("actuator")


class Actuator:
    def forward(self) -> None: ...   # 안쪽으로(상차)
    def reverse(self) -> None: ...   # 바깥쪽으로(하차)
    def stop(self) -> None: ...
    def on_estop_button(self, cb: Callable[[], None]) -> None: ...


class MockActuator(Actuator):
    def __init__(self) -> None:
        self.motion = "STOP"

    def forward(self) -> None:
        self.motion = "FWD"
        log.info("motor >> FORWARD (load)")

    def reverse(self) -> None:
        self.motion = "REV"
        log.info("motor << REVERSE (unload)")

    def stop(self) -> None:
        if self.motion != "STOP":
            log.info("motor || STOP")
        self.motion = "STOP"

    def on_estop_button(self, cb: Callable[[], None]) -> None:
        pass  # PC에서는 서버의 원격 ESTOP 또는 SIGUSR2로 시험


class GpioActuator(Actuator):
    """BCM 핀 기본값: 정방향 LED 17, 역방향 LED 27, 비상정지 버튼 22"""

    def __init__(self, fwd_pin: int = 17, rev_pin: int = 27, estop_pin: int = 22) -> None:
        from gpiozero import LED, Button  # 실보드에서만 import

        self._fwd = LED(fwd_pin)
        self._rev = LED(rev_pin)
        self._btn = Button(estop_pin, pull_up=True, bounce_time=0.05)

    def forward(self) -> None:
        self._rev.off()
        self._fwd.on()

    def reverse(self) -> None:
        self._fwd.off()
        self._rev.on()

    def stop(self) -> None:
        self._fwd.off()
        self._rev.off()

    def on_estop_button(self, cb: Callable[[], None]) -> None:
        self._btn.when_pressed = cb


def make_actuator(kind: str) -> Actuator:
    if kind == "gpio":
        return GpioActuator()
    return MockActuator()
