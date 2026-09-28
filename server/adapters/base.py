"""고객사 상위 시스템(WMS/TMS) 연동 어댑터.

고객사마다 WMS·TMS의 데이터 형식이 다르다. 오케스트레이터 코어가 그 차이를
알지 못하도록, 어댑터가 각 형식을 공통 모델(Normalized*)로 바꿔준다.
새 고객사가 생기면 어댑터 하나만 추가하면 된다. (docs/adr/0004 참고)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class NormalizedOrder:
    order_id: str
    dock: str
    pallets: int
    sku_summary: str


@dataclass
class NormalizedDispatch:
    truck: str
    dock: str
    eta: str


class AdapterError(ValueError):
    pass


def normalize_dock(raw: str | int) -> str:
    """'D-05', '5', 'DOCK5', 'Gate 05' -> 'D05'"""
    m = re.search(r"(\d+)", str(raw))
    if not m:
        raise AdapterError(f"dock number not found in {raw!r}")
    return f"D{int(m.group(1)):02d}"


WmsParser = Callable[[dict], NormalizedOrder]
TmsParser = Callable[[str], list[NormalizedDispatch]]

WMS_ADAPTERS: dict[str, WmsParser] = {}
TMS_ADAPTERS: dict[str, TmsParser] = {}


def wms_adapter(name: str):
    def deco(fn: WmsParser) -> WmsParser:
        WMS_ADAPTERS[name] = fn
        return fn
    return deco


def tms_adapter(name: str):
    def deco(fn: TmsParser) -> TmsParser:
        TMS_ADAPTERS[name] = fn
        return fn
    return deco
