"""현장 장비 위치 수신 (현장 관제 모드).

같은 프로토콜($POS ... *CS)을 세 가지 경로로 받는다.
- UDP  : 무선 모뎀/데이터링크가 데이터그램으로 보내는 경우
- TCP  : 게이트웨이가 스트림으로 붙는 경우 (줄 단위 프레이밍, 부분 수신 처리)
- 시리얼: RS-232C/USB-시리얼로 직접 연결된 장비 (pyserial, 별도 스레드)

수신 결과는 장비별 최신 위치 + 이동 궤적 + 통신 상태로 관리하고
웹소켓으로 오프라인 지도 화면에 전달한다.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from common.protocol import FrameError, decode_pos

log = logging.getLogger("field")

TRACK_LEN = 60


@dataclass
class Unit:
    unit: str
    lat: float = 0.0
    lon: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    source: str = ""
    last_seen: float = 0.0
    online: bool = True
    frames: int = 0
    track: deque = field(default_factory=lambda: deque(maxlen=TRACK_LEN))

    def to_dict(self) -> dict:
        return {"unit": self.unit, "lat": self.lat, "lon": self.lon, "speed": self.speed,
                "heading": self.heading, "source": self.source, "last_seen": self.last_seen,
                "online": self.online, "frames": self.frames, "track": list(self.track)}


class FieldTracker:
    def __init__(self, lost_after: float = 5.0, clock: Callable[[], float] = time.time) -> None:
        self.units: dict[str, Unit] = {}
        self.bad_frames = 0
        self.lost_after = lost_after
        self.now = clock
        self.version = 0
        self.log: deque = deque(maxlen=20)

    def feed(self, line: str, source: str) -> bool:
        if not line.strip():
            return False
        try:
            r = decode_pos(line)
        except FrameError as e:
            self.bad_frames += 1
            self.log.appendleft({"ts": self.now(), "msg": f"[{source}] 프레임 오류: {e}"})
            self.version += 1
            return False
        u = self.units.get(r.unit) or Unit(r.unit)
        if not u.online:
            self.log.appendleft({"ts": self.now(), "msg": f"{r.unit} 통신 복구 ({source})"})
        u.lat, u.lon, u.speed, u.heading = r.lat, r.lon, r.speed, r.heading
        u.source, u.last_seen, u.online = source, self.now(), True
        u.frames += 1
        u.track.append([r.lat, r.lon])
        self.units[r.unit] = u
        self.version += 1
        return True

    def sweep(self) -> bool:
        changed = False
        for u in self.units.values():
            if u.online and self.now() - u.last_seen > self.lost_after:
                u.online = False
                self.log.appendleft({"ts": self.now(), "msg": f"{u.unit} 통신 두절 ({self.lost_after:.0f}초)"})
                changed = True
        if changed:
            self.version += 1
        return changed

    def snapshot(self) -> dict:
        return {"units": [u.to_dict() for u in sorted(self.units.values(), key=lambda x: x.unit)],
                "bad_frames": self.bad_frames, "log": list(self.log), "ts": self.now()}


# ---------------------------------------------------------------------------- 수신기
class _UdpProto(asyncio.DatagramProtocol):
    def __init__(self, tracker: FieldTracker) -> None:
        self.t = tracker

    def datagram_received(self, data: bytes, addr) -> None:
        for line in data.decode("ascii", "replace").splitlines():
            self.t.feed(line, "UDP")


async def start_udp(tracker: FieldTracker, port: int):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _UdpProto(tracker), local_addr=("0.0.0.0", port))
    log.info("field UDP listening :%d", port)
    return transport


async def start_tcp(tracker: FieldTracker, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("field TCP client %s", peer)
        try:
            while True:
                line = await reader.readline()   # 줄 단위로 끊어 읽어 부분 수신에도 안전
                if not line:
                    break
                tracker.feed(line.decode("ascii", "replace"), "TCP")
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("field TCP listening :%d", port)
    return server


def start_serial(tracker: FieldTracker, device: str, baud: int, loop: asyncio.AbstractEventLoop) -> threading.Thread:
    """시리얼 포트는 블로킹 I/O라 별도 스레드에서 읽고, 파싱은 이벤트 루프로 넘긴다."""
    import serial  # pyserial

    def run() -> None:
        while True:
            try:
                with serial.Serial(device, baud, timeout=1) as port:
                    log.info("field SERIAL open %s @%d", device, baud)
                    while True:
                        raw = port.readline()
                        if raw:
                            loop.call_soon_threadsafe(tracker.feed, raw.decode("ascii", "replace"), "SERIAL")
            except Exception as e:  # 케이블 분리 등 -> 재시도
                log.warning("serial %s error: %s (3초 후 재시도)", device, e)
                time.sleep(3)

    th = threading.Thread(target=run, name="serial-reader", daemon=True)
    th.start()
    return th
