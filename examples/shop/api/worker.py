"""Shop worker: counts item events per branch in Redis. Fictional example for wt."""

import json
import os
import signal
import time
from pathlib import Path

import redis
from confluent_kafka import Consumer

KAFKA_BOOTSTRAP = os.environ["KAFKA_BOOTSTRAP"]
KAFKA_GROUP = os.environ["KAFKA_GROUP"]
KAFKA_TOPIC = os.environ["KAFKA_TOPIC"]
REDIS_URL = os.environ["REDIS_URL"]
REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "")
HEARTBEAT = Path("/tmp/worker-heartbeat")


def handle(r: redis.Redis, value: bytes) -> None:
    event = json.loads(value)
    r.incr(f"{REDIS_PREFIX}events")
    r.set(f"{REDIS_PREFIX}last", str(event.get("item", "")))


def run() -> None:
    r = redis.Redis.from_url(REDIS_URL)
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": KAFKA_GROUP,
            "auto.offset.reset": "earliest",
            "allow.auto.create.topics": True,
        }
    )
    try:
        consumer.subscribe([KAFKA_TOPIC])
        while True:
            HEARTBEAT.touch()
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                # Unknown topic until the first event creates it: keep polling.
                print(f"consumer: {msg.error()}", flush=True)
                continue
            handle(r, msg.value())
            consumer.commit(message=msg)
    finally:
        consumer.close()


def stop(signum: int, frame: object) -> None:
    # PID 1 ignores SIGTERM by default; exit so `finally` closes the consumer, which leaves
    # its group at once (a killed member keeps the group non-empty until its session expires,
    # and `wt down` cannot delete a non-empty group).
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    while True:
        try:
            run()
        except Exception as exc:  # reconnect while Kafka or Redis restarts
            print(f"worker: {exc!r}; retrying in 2s", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
