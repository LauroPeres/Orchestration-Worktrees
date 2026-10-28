"""Tiny shop API: items in Postgres. Fictional example for wt."""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
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

log = logging.getLogger("shop.api")
producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP}) if KAFKA_BOOTSTRAP else None


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
    quantity: int = 1


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"branch": BRANCH}


@app.get("/items")
def list_items() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, name, quantity FROM items ORDER BY id").fetchall()


@app.post("/items", status_code=201)
def create_item(item: NewItem) -> dict:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO items (name, quantity) VALUES (%s, %s) RETURNING id, name, quantity",
            (item.name, item.quantity),
        ).fetchone()
    publish_event(item.name)
    return row or {}
