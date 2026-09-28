"""관제 서버 (FastAPI).

실행: uvicorn server.app:app --port 8080
  /dock   도크 자동화 관제 화면
  /field  현장 장비 오프라인 지도 화면 (현장 관제 모드)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.protocol import Command, Event, parse_topic, topic_cmd
from server import config
from server.adapters import formats  # noqa: F401  (어댑터 등록)
from server.adapters.base import TMS_ADAPTERS, WMS_ADAPTERS, AdapterError, normalize_dock
from server.field import FieldTracker, start_serial, start_tcp, start_udp
from server.orchestrator import Orchestrator
from server.store import Store

log = logging.getLogger("server")
STATIC = Path(__file__).parent / "static"


class Hub:
    """웹소켓 구독자 관리. 변경된 tenant만 모아서 0.3초마다 전송한다."""

    def __init__(self) -> None:
        self.dock: dict[str, set[WebSocket]] = {}
        self.field: set[WebSocket] = set()
        self.dirty: set[str] = set()

    def touch(self, tenant: str) -> None:
        self.dirty.add(tenant)


class MqttBridge:
    def __init__(self, loop: asyncio.AbstractEventLoop, orch: Orchestrator, hub: Hub) -> None:
        import paho.mqtt.client as mqtt

        self.loop, self.orch, self.hub = loop, orch, hub
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dock-orchestrator")
        self.c.reconnect_delay_set(1, 10)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message

    def start(self) -> None:
        self.c.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=10)
        self.c.loop_start()

    def stop(self) -> None:
        self.c.loop_stop()

    def send(self, tenant: str, truck: str, cmd: Command) -> None:
        self.c.publish(topic_cmd(tenant, truck), cmd.to_json(), qos=1)
        log.info("cmd -> %s/%s %s job=%s pallets=%s", tenant, truck, cmd.type, cmd.job_id, cmd.pallets)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        client.subscribe([("dock/+/+/evt", 1), ("dock/+/+/status", 0)])
        log.info("MQTT connected %s:%d", config.MQTT_HOST, config.MQTT_PORT)

    def _on_message(self, client, userdata, msg) -> None:
        # paho 스레드 -> 이벤트 루프 스레드로 넘겨서 단일 스레드로 처리
        self.loop.call_soon_threadsafe(self._handle, msg.topic, bytes(msg.payload))

    def _handle(self, topic: str, payload: bytes) -> None:
        t = parse_topic(topic)
        if not t or t[0] not in config.TENANTS:
            return
        tenant, truck, kind = t
        try:
            if kind == "evt":
                changed = self.orch.on_event(tenant, Event.from_json(payload))
            elif kind == "status":
                changed = self.orch.on_status(tenant, truck, json.loads(payload))
            else:
                return
        except (ValueError, TypeError, KeyError) as e:
            log.warning("bad message on %s: %s", topic, e)
            return
        if changed:
            self.hub.touch(tenant)


# --------------------------------------------------------------------------- 앱 수명주기
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    loop = asyncio.get_running_loop()
    hub = Hub()
    bridge_ref: dict = {}
    orch = Orchestrator(Store(config.DB_PATH), lambda *a: bridge_ref["b"].send(*a),
                        heartbeat_timeout=config.HEARTBEAT_TIMEOUT, ack_timeout=config.ACK_TIMEOUT,
                        stall_timeout=config.STALL_TIMEOUT)
    bridge = MqttBridge(loop, orch, hub)
    bridge_ref["b"] = bridge
    bridge.start()

    tracker = FieldTracker(config.FIELD_LOST_AFTER)
    closers = []
    if config.FIELD_ENABLED:
        closers.append(await start_udp(tracker, config.FIELD_UDP_PORT))
        closers.append(await start_tcp(tracker, config.FIELD_TCP_PORT))
        if config.FIELD_SERIAL:
            start_serial(tracker, config.FIELD_SERIAL, config.FIELD_BAUD, loop)

    app.state.orch, app.state.hub, app.state.tracker = orch, hub, tracker
    tasks = [asyncio.create_task(_sweeper(orch, tracker, hub)),
             asyncio.create_task(_pusher(orch, tracker, hub))]
    yield
    for t in tasks:
        t.cancel()
    for c in closers:
        c.close()
    bridge.stop()


async def _sweeper(orch: Orchestrator, tracker: FieldTracker, hub: Hub) -> None:
    while True:
        await asyncio.sleep(1)
        if orch.sweep():
            hub.dirty.update(config.TENANTS)
        tracker.sweep()


async def _pusher(orch: Orchestrator, tracker: FieldTracker, hub: Hub) -> None:
    field_ver = -1
    while True:
        await asyncio.sleep(0.3)
        for tenant in list(hub.dirty):
            hub.dirty.discard(tenant)
            await _broadcast(hub.dock.get(tenant, set()), orch.snapshot(tenant))
        if tracker.version != field_ver:
            field_ver = tracker.version
            await _broadcast(hub.field, tracker.snapshot())


async def _broadcast(clients: set[WebSocket], data: dict) -> None:
    for ws in list(clients):
        try:
            await ws.send_json(data)
        except Exception:
            clients.discard(ws)


app = FastAPI(title="dock-orchestrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _auth(tenant: str, key: str | None) -> dict:
    t = config.TENANTS.get(tenant)
    if not t:
        raise HTTPException(404, "unknown tenant")
    if key != t["api_key"]:
        raise HTTPException(401, "invalid api key")
    return t


def _tenant(tenant: str) -> dict:
    t = config.TENANTS.get(tenant)
    if not t:
        raise HTTPException(404, "unknown tenant")
    return t


# --------------------------------------------------------------------------- 화면
@app.get("/")
def root():
    return RedirectResponse("/dock")


@app.get("/dock")
def dock_page():
    return FileResponse(STATIC / "dock.html")


@app.get("/field")
def field_page():
    return FileResponse(STATIC / "field.html")


# --------------------------------------------------------------------------- 상위 시스템 연동 API
@app.post("/api/{tenant}/wms/orders")
async def wms_order(tenant: str, request: Request, x_api_key: str | None = Header(None)):
    t = _auth(tenant, x_api_key)
    try:
        order = WMS_ADAPTERS[t["wms"]](await request.json())
    except (AdapterError, json.JSONDecodeError) as e:
        raise HTTPException(422, str(e))
    jobs = request.app.state.orch.on_order(tenant, order)
    request.app.state.hub.touch(tenant)
    return {"accepted": order.__dict__, "jobs_created": jobs}


@app.post("/api/{tenant}/tms/dispatches")
async def tms_dispatch(tenant: str, request: Request, x_api_key: str | None = Header(None)):
    t = _auth(tenant, x_api_key)
    try:
        rows = TMS_ADAPTERS[t["tms"]]((await request.body()).decode("utf-8"))
    except (AdapterError, UnicodeDecodeError) as e:
        raise HTTPException(422, str(e))
    jobs = [j for d in rows for j in request.app.state.orch.on_dispatch(tenant, d)]
    request.app.state.hub.touch(tenant)
    return {"accepted": len(rows), "jobs_created": jobs}


@app.post("/api/{tenant}/amr/state")
async def amr_state(tenant: str, request: Request, x_api_key: str | None = Header(None)):
    """AMR 상태 보고 (VDA 5050 state 메시지를 단순화한 형식).
    drop 액션이 FINISHED이고 nodeId가 도크면 '짐 도착'으로 본다."""
    _auth(tenant, x_api_key)
    body = await request.json()
    finished = any(a.get("actionType") == "drop" and a.get("actionStatus") == "FINISHED"
                   for a in body.get("actionStates", []))
    if not finished:
        return {"ignored": True}
    try:
        dock = normalize_dock(body["nodeId"])
    except (KeyError, AdapterError) as e:
        raise HTTPException(422, str(e))
    job = request.app.state.orch.on_amr_arrival(tenant, dock, body.get("orderId"))
    request.app.state.hub.touch(tenant)
    return {"dock": dock, "job": job}


# --------------------------------------------------------------------------- 운영자 조작 (관제 화면)
@app.post("/api/{tenant}/trucks/{truck}/estop")
def estop(tenant: str, truck: str, request: Request):
    _tenant(tenant)
    request.app.state.orch.remote_estop(tenant, truck)
    return {"sent": "ESTOP"}


@app.post("/api/{tenant}/trucks/{truck}/reset")
def reset(tenant: str, truck: str, request: Request):
    _tenant(tenant)
    request.app.state.orch.remote_reset(tenant, truck)
    return {"sent": "RESET"}


@app.post("/api/{tenant}/jobs/{job_id}/resume")
def resume(tenant: str, job_id: str, request: Request):
    _tenant(tenant)
    job = request.app.state.orch.resume(tenant, job_id)
    if not job:
        raise HTTPException(409, "job is not HALTED")
    request.app.state.hub.touch(tenant)
    return job


@app.get("/api/tenants")
def tenants():
    return [{"id": k, "name": v["name"]} for k, v in config.TENANTS.items()]


@app.get("/api/{tenant}/snapshot")
def snapshot(tenant: str, request: Request):
    _tenant(tenant)
    return request.app.state.orch.snapshot(tenant)


@app.get("/api/{tenant}/usage")
def usage(tenant: str, request: Request):
    """월별 사용량 - 구독형 과금(작업 건수/팔레트 수 기준)의 근거 데이터"""
    _tenant(tenant)
    return {"tenant": tenant, "usage": request.app.state.orch.usage(tenant)}


@app.websocket("/ws/dock/{tenant}")
async def ws_dock(ws: WebSocket, tenant: str):
    if tenant not in config.TENANTS:
        await ws.close(code=4404)
        return
    await ws.accept()
    hub: Hub = ws.app.state.hub
    hub.dock.setdefault(tenant, set()).add(ws)
    await ws.send_json(ws.app.state.orch.snapshot(tenant))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.dock[tenant].discard(ws)


# --------------------------------------------------------------------------- 현장 지도 (현장 관제 모드)
@app.get("/api/field/units")
def field_units(request: Request):
    return request.app.state.tracker.snapshot()


@app.websocket("/ws/field")
async def ws_field(ws: WebSocket):
    await ws.accept()
    ws.app.state.hub.field.add(ws)
    await ws.send_json(ws.app.state.tracker.snapshot())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws.app.state.hub.field.discard(ws)


@app.get("/api/field/tiles/info")
def tiles_info():
    path = config.MBTILES_PATH
    if not os.path.exists(path):
        return {"available": False}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        meta = dict(db.execute("SELECT name, value FROM metadata").fetchall())
    return {"available": True, **{k: meta.get(k) for k in ("name", "minzoom", "maxzoom", "bounds", "center")}}


@app.get("/tiles/{z}/{x}/{y}.png")
def tile(z: int, x: int, y: int):
    """MBTiles(SQLite) 파일에서 타일을 꺼내 준다. 인터넷 연결 불필요.
    MBTiles는 TMS 좌표계라 y를 뒤집는다."""
    path = config.MBTILES_PATH
    if not os.path.exists(path):
        raise HTTPException(404)
    tms_y = (1 << z) - 1 - y
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                         (z, x, tms_y)).fetchone()
    if not row:
        return Response(status_code=204)
    return Response(row[0], media_type="image/png", headers={"Cache-Control": "max-age=86400"})
