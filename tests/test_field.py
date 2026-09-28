import asyncio
import socket
import sqlite3

import pytest

from common.protocol import FrameError, decode_pos, encode_pos
from server.field import FieldTracker, start_tcp, start_udp


def test_pos_roundtrip():
    line = encode_pos("UNIT-01", 37.391512, 126.645034, 12.5, 90)
    r = decode_pos(line)
    assert (r.unit, round(r.lat, 6), round(r.lon, 6), r.speed, r.heading) == ("UNIT-01", 37.391512, 126.645034, 12.5, 90)


@pytest.mark.parametrize("bad", [
    "POS,U,1,2,3,4*00",                    # $ 없음
    "$POS,U,1,2,3,4",                      # 체크섬 없음
    "$POS,U,1,2,3,4*00",                   # 체크섬 불일치
    "$POS,U,999,2,3,4*" + "00",            # 범위 밖 (체크섬도 틀림)
])
def test_bad_frames_rejected(bad):
    with pytest.raises(FrameError):
        decode_pos(bad)


def test_out_of_range_with_valid_checksum():
    from common.protocol import nmea_checksum
    body = "POS,U,95.0,126.0,1.0,0"
    with pytest.raises(FrameError):
        decode_pos(f"${body}*{nmea_checksum(body)}")


def test_tracker_comm_loss_and_recovery():
    now = [0.0]
    t = FieldTracker(lost_after=5, clock=lambda: now[0])
    assert t.feed(encode_pos("U1", 37.0, 126.0, 0, 0), "UDP")
    assert not t.feed("garbage", "UDP")
    assert t.bad_frames == 1
    now[0] = 6
    assert t.sweep()
    assert t.snapshot()["units"][0]["online"] is False
    t.feed(encode_pos("U1", 37.0001, 126.0, 0, 0), "TCP")
    u = t.snapshot()["units"][0]
    assert u["online"] and u["source"] == "TCP" and len(u["track"]) == 2


def _free_port(kind):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_udp_and_tcp_receivers():
    async def run():
        t = FieldTracker()
        up, tp = _free_port(socket.SOCK_DGRAM), _free_port(socket.SOCK_STREAM)
        udp = await start_udp(t, up)
        tcp = await start_tcp(t, tp)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto((encode_pos("A", 37.0, 126.0, 1, 1) + "\n").encode(), ("127.0.0.1", up))
        r, w = await asyncio.open_connection("127.0.0.1", tp)
        line = encode_pos("B", 37.1, 126.1, 1, 1) + "\n"
        w.write(line[:10].encode())            # 프레임이 두 번에 나뉘어 도착하는 경우
        await w.drain()
        await asyncio.sleep(0.05)
        w.write(line[10:].encode())
        await w.drain()
        await asyncio.sleep(0.2)
        w.close()
        udp.close()
        tcp.close()
        return {u["unit"]: u["source"] for u in t.snapshot()["units"]}

    assert asyncio.run(run()) == {"A": "UDP", "B": "TCP"}


def test_mbtiles_tile_route(tmp_path, monkeypatch):
    """MBTiles 파일에서 타일을 꺼내는 경로 (TMS y 뒤집기 확인)"""
    path = tmp_path / "t.mbtiles"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE metadata(name TEXT, value TEXT);"
                     "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB);")
    db.execute("INSERT INTO metadata VALUES('name','test'),('minzoom','1'),('maxzoom','1')")
    db.execute("INSERT INTO tiles VALUES(1, 0, 1, ?)", (b"PNGDATA",))   # TMS row 1 == XYZ y 0
    db.commit()
    db.close()

    from server import app as appmod
    monkeypatch.setattr(appmod.config, "MBTILES_PATH", str(path))
    assert appmod.tile(1, 0, 0).body == b"PNGDATA"
    assert appmod.tile(1, 0, 1).status_code == 204
    assert appmod.tiles_info()["name"] == "test"
