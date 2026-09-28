"""가상의 고객사 2곳의 형식 예시.

- json_v1 : A사 WMS. 팔레트 단위 주문, 도크를 'dockDoor'로 표기
- json_v2 : B사 WMS. 박스 단위 주문(40박스=1팔레트로 환산), 도크를 'gate'로 표기
- csv_v1  : 공통 TMS 배차 CSV (truck_no,dock,eta)
"""
from __future__ import annotations

import csv
import io
import math

from server.adapters.base import (
    AdapterError,
    NormalizedDispatch,
    NormalizedOrder,
    normalize_dock,
    tms_adapter,
    wms_adapter,
)

BOXES_PER_PALLET = 40


@wms_adapter("json_v1")
def parse_wms_v1(body: dict) -> NormalizedOrder:
    """{"outboundOrder": {"orderNo": "SO-1001", "dockDoor": "D-05",
                          "lines": [{"sku": "A-1", "palletQty": 3}]}}"""
    try:
        o = body["outboundOrder"]
        lines = o["lines"]
        pallets = sum(int(l["palletQty"]) for l in lines)
        skus = ",".join(l["sku"] for l in lines)
        return NormalizedOrder(str(o["orderNo"]), normalize_dock(o["dockDoor"]), pallets, skus)
    except (KeyError, TypeError, ValueError) as e:
        raise AdapterError(f"json_v1 parse error: {e}") from e


@wms_adapter("json_v2")
def parse_wms_v2(body: dict) -> NormalizedOrder:
    """{"order_id": "B-77", "gate": "Gate 3",
        "items": [{"code": "X", "qty": 60, "unit": "BOX"}, {"code": "Y", "qty": 1, "unit": "PLT"}]}"""
    try:
        pallets = 0.0
        for it in body["items"]:
            qty = float(it["qty"])
            unit = it.get("unit", "PLT").upper()
            if unit == "BOX":
                pallets += qty / BOXES_PER_PALLET
            elif unit == "PLT":
                pallets += qty
            else:
                raise AdapterError(f"unknown unit {unit}")
        skus = ",".join(it["code"] for it in body["items"])
        return NormalizedOrder(str(body["order_id"]), normalize_dock(body["gate"]),
                               max(1, math.ceil(pallets)), skus)
    except (KeyError, TypeError, ValueError) as e:
        raise AdapterError(f"json_v2 parse error: {e}") from e


@tms_adapter("csv_v1")
def parse_tms_csv(text: str) -> list[NormalizedDispatch]:
    """truck_no,dock,eta
       TRK-01,5,2026-09-24T09:00"""
    out: list[NormalizedDispatch] = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames or not {"truck_no", "dock"} <= set(reader.fieldnames):
        raise AdapterError("csv_v1 requires columns truck_no,dock[,eta]")
    for row in reader:
        out.append(NormalizedDispatch(row["truck_no"].strip(), normalize_dock(row["dock"]),
                                      (row.get("eta") or "").strip()))
    return out
