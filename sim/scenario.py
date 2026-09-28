"""상위 시스템(WMS/TMS)과 로봇(AMR)을 흉내 내어 도크 작업 한 사이클을 발생시킨다.

    python -m sim.scenario --tenant acme --truck TRK-01 --dock 5
    python -m sim.scenario --tenant hanbit --truck TRK-02 --dock 3

순서: WMS 출고 오더 -> TMS 배차 -> (대기) -> AMR 짐 도착 보고
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request

KEYS = {"acme": "acme-demo-key", "hanbit": "hanbit-demo-key"}


def post(base: str, path: str, key: str, body: bytes, ctype: str) -> dict:
    req = urllib.request.Request(f"{base}{path}", data=body, method="POST",
                                 headers={"Content-Type": ctype, "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8080")
    p.add_argument("--tenant", default="acme")
    p.add_argument("--truck", default="TRK-01")
    p.add_argument("--dock", type=int, default=5)
    p.add_argument("--wait", type=float, default=2.0, help="배차 후 AMR 도착까지 대기(초)")
    a = p.parse_args()
    key = KEYS[a.tenant]
    n = random.randint(1000, 9999)

    if a.tenant == "acme":   # A사: 팔레트 단위 JSON
        order = {"outboundOrder": {"orderNo": f"SO-{n}", "dockDoor": f"D-{a.dock:02d}",
                                   "lines": [{"sku": "A-100", "palletQty": 3}, {"sku": "A-200", "palletQty": 2}]}}
    else:                    # B사: 박스 단위 JSON (어댑터가 팔레트로 환산)
        order = {"order_id": f"B-{n}", "gate": f"Gate {a.dock}",
                 "items": [{"code": "X-1", "qty": 80, "unit": "BOX"}, {"code": "Y-2", "qty": 2, "unit": "PLT"}]}

    print("[WMS] 출고 오더 ->", post(a.base, f"/api/{a.tenant}/wms/orders", key,
                                    json.dumps(order).encode(), "application/json")["accepted"])
    csv = f"truck_no,dock,eta\n{a.truck},{a.dock},{time.strftime('%Y-%m-%dT%H:%M')}\n"
    r = post(a.base, f"/api/{a.tenant}/tms/dispatches", key, csv.encode(), "text/csv")
    print("[TMS] 배차 -> 작업 생성", [j["job_id"] for j in r["jobs_created"]])
    time.sleep(a.wait)
    amr = {"serialNumber": "AMR-07", "orderId": None, "nodeId": f"D{a.dock:02d}",
           "actionStates": [{"actionType": "drop", "actionStatus": "FINISHED"}]}
    r = post(a.base, f"/api/{a.tenant}/amr/state", key, json.dumps(amr).encode(), "application/json")
    print("[AMR] 짐 도착 -> 적재 지시", r.get("job"))


if __name__ == "__main__":
    main()
