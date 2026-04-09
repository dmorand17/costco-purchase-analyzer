"""FastAPI application — Lambda handler via Mangum ASGI adapter."""

import os
from pathlib import Path
from typing import Annotated

import boto3
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mangum import Mangum

from services import analyzer, db
from services.price_scanner import scan_price_drops
from services.receipt_parser import parse_receipt_pdf, reparse_receipt

app = FastAPI(title="Costco Purchase Analyzer")

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

MAX_PDF_SIZE = 10 * 1024 * 1024  # 10 MB


# ── Frontend ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text())
    return HTMLResponse(content="<h1>Costco Purchase Analyzer</h1>")


# ── Receipt Upload & Parsing ───────────────────────────────────────────────────


@app.post("/api/upload")
async def upload_receipt(file: UploadFile = File(...)):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_PDF_SIZE:
        raise HTTPException(status_code=413, detail="PDF exceeds 10 MB limit.")
    parsed = parse_receipt_pdf(pdf_bytes)
    receipt = db.put_receipt(parsed, pdf_bytes)
    return receipt


@app.post("/api/reparse/{receipt_id}")
async def reparse_receipt_endpoint(receipt_id: str):
    pdf_bytes = db.get_pdf(receipt_id)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="Receipt PDF not found.")
    parsed = reparse_receipt(pdf_bytes)
    db.update_receipt(
        receipt_id,
        {
            "items": parsed["items"],
            "receipt_date": parsed.get("receipt_date", ""),
            "store": parsed.get("store", ""),
        },
    )
    return db.get_receipt(receipt_id)


# ── Receipts CRUD ──────────────────────────────────────────────────────────────


@app.get("/api/receipts")
async def list_receipts():
    return db.list_receipts()


@app.delete("/api/receipts")
async def clear_receipts():
    db.clear_receipts()
    return {"status": "cleared"}


@app.delete("/api/receipt/{receipt_id}")
async def delete_receipt(receipt_id: str):
    db.delete_receipt(receipt_id)
    return {"status": "deleted"}


@app.get("/api/receipt/{receipt_id}/pdf")
async def get_receipt_pdf(receipt_id: str):
    pdf_bytes = db.get_pdf(receipt_id)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="PDF not found.")
    return Response(content=pdf_bytes, media_type="application/pdf")


@app.put("/api/receipt/{receipt_id}/item/{item_index}")
async def update_receipt_item(receipt_id: str, item_index: int, request: Request):
    body = await request.json()
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    items = receipt.get("items", [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=404, detail="Item index out of range.")
    items[item_index].update(body)
    db.update_receipt(receipt_id, {"items": items})
    return items[item_index]


# ── Price Drop Scanning ────────────────────────────────────────────────────────


@app.post("/api/scan-prices")
async def trigger_price_scan():
    items = scan_price_drops(force_refresh=True)
    count = db.put_price_drops(items)
    return {"status": "scanned", "items_stored": count}


@app.get("/api/price-drops")
async def list_price_drops():
    return db.list_price_drops()


@app.delete("/api/price-drops")
async def clear_price_drops():
    db.clear_price_drops()
    return {"status": "cleared"}


@app.delete("/api/price-drop/{item_id}")
async def delete_price_drop(item_id: str):
    db.delete_price_drop(item_id)
    return {"status": "deleted"}


# ── AI Analysis (SSE streaming) ────────────────────────────────────────────────


@app.get("/api/analyze")
async def analyze(
    receipt_id: Annotated[str | None, Query()] = None,
    receipt_ids: Annotated[list[str] | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    sources: Annotated[list[str] | None, Query()] = None,
):
    ids: list[str] | None = None
    if receipt_id:
        ids = [receipt_id]
    elif receipt_ids:
        ids = receipt_ids

    return StreamingResponse(
        analyzer.run_analysis_stream(
            receipt_ids=ids,
            date_from=date_from,
            date_to=date_to,
            sources=sources,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Lambda handler ─────────────────────────────────────────────────────────────
handler = Mangum(app, lifespan="off")
