from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.queue import redis_conn
from app.routes import ops, talks, ui

app = FastAPI(title="VEditor API")

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

app.include_router(ops.router)
app.include_router(talks.router)
app.include_router(ui.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui")


@app.get("/health")
def health_check():
    redis_conn.ping()
    return {"status": "ok"}
