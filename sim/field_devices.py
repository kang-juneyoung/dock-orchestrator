"""현장 장비 3대를 흉내 내어 위치를 송신한다 (현장 관제 모드 시연용).

    python -m sim.field_devices                       # UNIT-01 UDP, UNIT-02 TCP, UNIT-03 UDP
    python -m sim.field_devices --serial /tmp/ttyV0   # UNIT-03을 시리얼로 송신
    python -m sim.field_devices --drop UNIT-02 --drop-after 20   # 20초 후 UNIT-02 송신 중단(통신 두절 시연)
    python -m sim.field_devices --noise 0.05          # 5% 확률로 체크섬 깨진 프레임 섞기

가상 시리얼 포트 만들기 (Linux/macOS):
    socat -d -d pty,raw,echo=0,link=/tmp/ttyV0 pty,raw,echo=0,link=/tmp/ttyV1
    서버: FIELD_SERIAL=/tmp/ttyV1  /  시뮬레이터: --serial /tmp/ttyV0
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import time

from common.protocol import encode_pos

CENTER = (37.3915, 126.6450)


class Mover:
    """원 궤도를 도는 장비"""

    def __init__(self, unit: str, radius_m: float, period_s: float, phase: float) -> None:
        self.unit, self.r, self.T, self.ph = unit, radius_m, period_s, phase

    def at(self, t: float) -> tuple[float, float, float, float]:
        a = 2 * math.pi * t / self.T + self.ph
        dlat = self.r * math.cos(a) / 111_320
        dlon = self.r * math.sin(a) / (111_320 * math.cos(math.radians(CENTER[0])))
        speed = 2 * math.pi * self.r / self.T * 3.6
        heading = (math.degrees(a) + 90) % 360
        return CENTER[0] + dlat, CENTER[1] + dlon, speed, heading


def corrupt(line: str) -> str:
    return line[:-2] + ("00" if line[-2:] != "00" else "FF")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--udp", type=int, default=5600)
    p.add_argument("--tcp", type=int, default=5601)
    p.add_argument("--serial", default="")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--hz", type=float, default=1.0)
    p.add_argument("--drop", default="")
    p.add_argument("--drop-after", type=float, default=20)
    p.add_argument("--noise", type=float, default=0.0)
    a = p.parse_args()

    movers = [Mover("UNIT-01", 300, 120, 0), Mover("UNIT-02", 520, 200, 2.0), Mover("UNIT-03", 180, 90, 4.0)]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tcp = socket.create_connection((a.host, a.tcp))
    ser = None
    if a.serial:
        import serial
        ser = serial.Serial(a.serial, a.baud)
    route = {"UNIT-01": "UDP", "UNIT-02": "TCP", "UNIT-03": "SERIAL" if ser else "UDP"}
    print("routes:", route)

    t0 = time.time()
    while True:
        t = time.time() - t0
        for m in movers:
            if m.unit == a.drop and t > a.drop_after:
                continue
            line = encode_pos(m.unit, *m.at(t))
            if a.noise and random.random() < a.noise:
                line = corrupt(line)
            data = (line + "\n").encode("ascii")
            via = route[m.unit]
            if via == "UDP":
                udp.sendto(data, (a.host, a.udp))
            elif via == "TCP":
                tcp.sendall(data)
            else:
                ser.write(data)
        time.sleep(1 / a.hz)


if __name__ == "__main__":
    main()
