"""Docker 없이 로컬에서 쓰는 MQTT 브로커 (amqtt, 순수 파이썬).
운영/도커 환경에서는 eclipse-mosquitto를 쓴다 (docker-compose.yml)."""
import asyncio
import logging

from amqtt.broker import Broker

CONFIG = {
    "listeners": {"default": {"type": "tcp", "bind": "0.0.0.0:1883"}},
    "sys_interval": 0,
    "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
    "topic-check": {"enabled": False},
}


async def main() -> None:
    broker = Broker(CONFIG)
    await broker.start()
    logging.info("MQTT broker on :1883")
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for noisy in ("transitions", "amqtt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main())
