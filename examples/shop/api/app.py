"""Tiny shop API: items in Postgres. Fictional example for wt."""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
BRANCH = os.environ.get("BRANCH", "unknown")
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", "")


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
    return row or {}
