"""Tiny notes API: notes in Postgres. Fictional example for wt."""

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
FRONT_ORIGIN = os.environ.get("FRONT_ORIGIN", "")


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
    allow_origins=[FRONT_ORIGIN] if FRONT_ORIGIN else [],
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


class NewNote(BaseModel):
    text: str
    tags: list[str] = []


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/info")
def info() -> dict:
    with connect() as conn:
        row = conn.execute("SELECT current_database() AS database").fetchone()
    return {"branch": BRANCH, "database": row["database"] if row else None}


@app.get("/notes")
def list_notes() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, text, tags FROM notes ORDER BY id").fetchall()


@app.post("/notes", status_code=201)
def create_note(note: NewNote) -> dict:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO notes (text, tags) VALUES (%s, %s) RETURNING id, text, tags",
            (note.text, note.tags),
        ).fetchone()
    return row or {}
