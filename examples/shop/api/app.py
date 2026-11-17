"""Tiny shop API: items in Postgres. Fictional example for wt."""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import redis
from confluent_kafka import Producer
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
BRANCH = os.environ.get("BRANCH", "unknown")
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", "")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "")
WORKER_KAFKA_GROUP = os.environ.get("WORKER_KAFKA_GROUP", "")
REDIS_URL = os.environ.get("REDIS_URL", "")
REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "")
RECENT_ITEMS = 5  # how many consumed items the worker keeps in `{prefix}recent`

log = logging.getLogger("shop.api")
producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP}) if KAFKA_BOOTSTRAP else None
# Short timeouts: a Redis outage must make /info answer fast with empty values, not hang.
cache = (
    redis.Redis.from_url(
        REDIS_URL, decode_responses=True, socket_connect_timeout=1, socket_timeout=1
    )
    if REDIS_URL
    else None
)


def publish_event(name: str) -> None:
    """Publish an item event; a failure is logged and never breaks the request."""
    if producer is None or not KAFKA_TOPIC:
        return
    try:
        payload = json.dumps({"item": name, "branch": BRANCH})
        producer.produce(KAFKA_TOPIC, payload.encode())
        if producer.flush(5) > 0:
            log.warning("event not delivered to %s", KAFKA_TOPIC)
    except Exception:
        log.exception("could not publish event")


def worker_feed() -> tuple[int, list[str]]:
    """What the worker wrote for this branch: its event count and last items, newest first.

    Redis being unreachable is logged and reads as "nothing yet", so /info still answers.
    """
    if cache is None:
        return 0, []
    try:
        events = cache.get(f"{REDIS_PREFIX}events")
        recent = cache.lrange(f"{REDIS_PREFIX}recent", 0, RECENT_ITEMS - 1)
    except redis.RedisError:
        log.exception("could not read the worker feed from Redis")
        return 0, []
    return int(events or 0), list(recent)


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def apply_schema() -> None:
    """Apply schema.sql, retrying while the database is being cloned or restarted."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    for attempt in range(30):
        try:
            with connect() as conn:
                conn.execute(sql)
            return
        except psycopg.OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    apply_schema()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN] if WEB_ORIGIN else [],
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


class NewItem(BaseModel):
    name: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"branch": BRANCH}


@app.get("/info")
def info() -> dict:
    with connect() as conn:
        database = conn.execute("SELECT current_database() AS name").fetchone()
    events, recent = worker_feed()
    return {
        "branch": BRANCH,
        "database": database["name"] if database else "",
        "redis_prefix": REDIS_PREFIX,
        "kafka_topic": KAFKA_TOPIC,
        "kafka_group": WORKER_KAFKA_GROUP,
        "events": events,
        "recent": recent,
    }


@app.get("/items")
def list_items() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, name FROM items ORDER BY id").fetchall()


@app.post("/items", status_code=201)
def create_item(item: NewItem) -> dict:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO items (name) VALUES (%s) RETURNING id, name", (item.name,)
        ).fetchone()
    publish_event(item.name)
    return row or {}
