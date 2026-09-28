"""고객사(tenant) 설정. 실제 서비스라면 DB에 둔다."""
from __future__ import annotations

import os

TENANTS: dict[str, dict] = {
    "acme": {"name": "A사 물류센터", "api_key": "acme-demo-key", "wms": "json_v1", "tms": "csv_v1"},
    "hanbit": {"name": "B사 풀필먼트", "api_key": "hanbit-demo-key", "wms": "json_v2", "tms": "csv_v1"},
}

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
DB_PATH = os.getenv("DB_PATH", "orchestrator.db")

# 경보 기준(초)
HEARTBEAT_TIMEOUT = float(os.getenv("HEARTBEAT_TIMEOUT", "5"))
ACK_TIMEOUT = float(os.getenv("ACK_TIMEOUT", "4"))
STALL_TIMEOUT = float(os.getenv("STALL_TIMEOUT", "15"))

# 현장 관제 모드: 현장 장비 위치 수신
FIELD_ENABLED = os.getenv("FIELD_ENABLED", "1") == "1"
FIELD_UDP_PORT = int(os.getenv("FIELD_UDP_PORT", "5600"))
FIELD_TCP_PORT = int(os.getenv("FIELD_TCP_PORT", "5601"))
FIELD_SERIAL = os.getenv("FIELD_SERIAL", "")          # 예: /dev/ttyUSB0
FIELD_BAUD = int(os.getenv("FIELD_BAUD", "9600"))
FIELD_LOST_AFTER = float(os.getenv("FIELD_LOST_AFTER", "5"))
MBTILES_PATH = os.getenv("MBTILES_PATH", "data/map.mbtiles")
