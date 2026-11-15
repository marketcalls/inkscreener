"""InkScreener FastAPI app.

Run from the repository root:
    python -m uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from . import builder, universes
from .config import AUTO_DOWNLOAD_INDICES, HISTORIFY_DB_PATH, STATIC_DIR
from .datastore import DataStore
from .expr import reference
from .scan import (
    PERIODS,
    ScanDef,
    ScanError,
    list_scans,
    load_scan,
    run_scan,
    save_scan,
    scan_history,
    with_trees,
)

log = logging.getLogger("inkscreener")
store = DataStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First start: fetch the Nifty index lists from NSE if none are present yet.
    if AUTO_DOWNLOAD_INDICES and not any(universes.local_path(uid).exists() for uid in universes.INDEX_FILES):
        try:
            for uid, info in universes.download_all().items():
                log.info("Index list %s: %s", uid, info.get("status"))
        except Exception as exc:
            log.warning("Could not download index lists from NSE: %s", exc)
    if not store.loaded:
        try:
            store.load()
        except Exception as exc:  # keep serving so /api/status can report the problem
            store.error = str(exc)
            log.error("Historify load failed: %s", exc)
    yield


app = FastAPI(title="InkScreener", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- helpers


class ScanRequest(BaseModel):
    scan_id: str | None = None
    scan: ScanDef | None = None
    universe: str | None = None
    as_of: str | None = None
    period: str = "9m"


def _resolve(req: ScanRequest) -> ScanDef:
    if req.scan is not None:
        return req.scan
    if req.scan_id:
        return _load(req.scan_id)
    raise HTTPException(400, detail={"message": "Send either 'scan' or 'scan_id'"})


def _load(scan_id: str) -> ScanDef:
    try:
        return load_scan(scan_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"message": str(exc)}) from None
    except (ValidationError, ScanError) as exc:
        raise HTTPException(400, detail={"message": f"Invalid scan file: {exc}"}) from None


def _require_data() -> None:
    if not store.loaded:
        raise HTTPException(503, detail={"message": store.error or "Historify data is not loaded"})


@app.exception_handler(ScanError)
async def _scan_error(_: Request, exc: ScanError):
    return JSONResponse(status_code=400, content={"detail": {"message": exc.message, "path": exc.path,
                                                             "column": exc.column}})


# --------------------------------------------------------------------------- pages


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------- data


@app.get("/api/status")
def status():
    return {
        "loaded": store.loaded,
        "error": store.error,
        "db_path": str(HISTORIFY_DB_PATH),
        "data": store.data.summary() if store.loaded else None,
        "periods": list(PERIODS),
    }


@app.post("/api/reload")
def reload_data():
    try:
        data = store.load()
    except Exception as exc:
        store.error = str(exc)
        raise HTTPException(503, detail={"message": str(exc)}) from None
    return data.summary()


@app.get("/api/universes")
def get_universes():
    out = universes.list_universes()
    if store.loaded:
        available = set(store.data.symbols)
        for u in out:
            if u["id"] == universes.ALL_UNIVERSE:
                u["size"] = u["in_data"] = len(available)
            elif u["available"]:
                members = [c.symbol for c in universes.load(u["id"])]
                u["size"] = len(members)
                u["in_data"] = sum(s in available for s in members)
                u["missing"] = [s for s in members if s not in available]
    return out


@app.post("/api/universes/refresh")
def refresh_universes():
    report = universes.download_all()
    store.clear_subsets()
    return report


@app.get("/api/reference")
def get_reference():
    return reference()


@app.get("/api/builder")
def get_builder():
    """Everything the point-and-click builder needs: measures, operators, quick filters."""
    return builder.palette()


# --------------------------------------------------------------------------- scans


@app.get("/api/scans")
def get_scans():
    return list_scans()


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str):
    return with_trees(_load(scan_id))


@app.put("/api/scans/{scan_id}")
def put_scan(scan_id: str, scan: ScanDef):
    save_scan(scan_id, scan)
    return {"saved": scan_id}


@app.post("/api/run")
def run(req: ScanRequest):
    _require_data()
    return run_scan(_resolve(req), store, as_of=req.as_of, universe=req.universe)


@app.post("/api/history")
def history(req: ScanRequest):
    _require_data()
    return scan_history(_resolve(req), store, period=req.period, universe=req.universe, as_of=req.as_of)


@app.get("/api/scans/{scan_id}/results.csv")
def results_csv(scan_id: str, universe: str | None = Query(None), as_of: str | None = Query(None)):
    _require_data()
    result = run_scan(_load(scan_id), store, as_of=as_of, universe=universe)
    labels = [c["label"] for c in result["columns"]]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["symbol", "company", "industry", *labels])
    for row in result["rows"]:
        writer.writerow([row["symbol"], row["company"], row["industry"], *[row["values"][lab] for lab in labels]])
    name = f"{scan_id}_{result['as_of']}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
