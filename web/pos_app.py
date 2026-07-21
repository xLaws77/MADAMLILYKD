"""
pos_app.py

FastAPI POS (Point of Sale) web application untuk Madam Lily.
Menggantikan keepalive.py sebagai HTTP server utama.

Routes:
  GET  /              → ping (UptimeRobot)
  GET  /pos           → kasir POS interface
  GET  /kitchen       → antrian dapur (kitchen display)
  GET  /admin         → dashboard admin
  GET  /admin/tables  → manajemen meja
  GET  /order         → customer self-order dari QR (/?meja=A1)
  GET  /qr/{table_no} → QR code PNG untuk meja

API (JSON):
  GET  /api/menu
  GET  /api/queue
  POST /api/pos/order
  POST /api/queue/{id}/status
  GET  /api/stats
  GET  /api/tables
  POST /api/tables
  DELETE /api/tables/{id}
"""

import io
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# DB layer
sys.path.insert(0, str(Path(__file__).parent.parent))
from web.pos_db import PosDB

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

db = PosDB()

app = FastAPI(title="Madam Lily POS", docs_url=None, redoc_url=None)


# ================================================================
# UTILITY
# ================================================================

def _format_riel(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _today() -> str:
    try:
        from app.timezone_utils import now_jakarta
        return now_jakarta().strftime("%Y-%m-%d")
    except ImportError:
        from datetime import date
        return date.today().isoformat()


# ================================================================
# PING (untuk UptimeRobot / Render health check)
# ================================================================

@app.get("/", response_class=HTMLResponse)
async def ping():
    return HTMLResponse("Madam Lily POS is running")


# ================================================================
# KASIR POS
# ================================================================

@app.get("/pos", response_class=HTMLResponse)
async def pos_page(request: Request):
    tables = db.get_tables()
    categories = db.get_categories()
    return templates.TemplateResponse("pos.html", {
        "request": request,
        "tables": tables,
        "categories": categories,
        "active": "pos",
    })


# ================================================================
# KITCHEN QUEUE
# ================================================================

@app.get("/kitchen", response_class=HTMLResponse)
async def kitchen_page(request: Request):
    return templates.TemplateResponse("kitchen.html", {
        "request": request,
        "active": "kitchen",
    })


# ================================================================
# ADMIN DASHBOARD
# ================================================================

@app.get("/admin", response_class=HTMLResponse)
async def dashboard_page(request: Request, tanggal: Optional[str] = None):
    today = tanggal or _today()
    stats = db.stats_today(today)
    done_orders = db.get_done_orders(today)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "done_orders": done_orders,
        "today": today,
        "format_riel": _format_riel,
        "active": "admin",
    })


# ================================================================
# ADMIN TABLES
# ================================================================

@app.get("/admin/tables", response_class=HTMLResponse)
async def tables_page(request: Request):
    tables = db.get_tables()
    base_url = os.getenv("BASE_URL", "").rstrip("/")
    return templates.TemplateResponse("tables.html", {
        "request": request,
        "tables": tables,
        "base_url": base_url,
        "active": "tables",
    })


# ================================================================
# CUSTOMER SELF-ORDER (dari scan QR)
# ================================================================

@app.get("/order", response_class=HTMLResponse)
async def self_order_page(request: Request, meja: str = ""):
    categories = db.get_categories()
    return templates.TemplateResponse("self_order.html", {
        "request": request,
        "meja": meja.upper(),
        "categories": categories,
    })


# ================================================================
# QR CODE GENERATOR
# ================================================================

@app.get("/qr/{table_no}")
async def qr_code(table_no: str):
    try:
        import qrcode
        from PIL import Image
    except ImportError:
        raise HTTPException(500, "Install qrcode[pil] untuk generate QR")

    base_url = os.getenv("BASE_URL", "http://localhost:8080").rstrip("/")
    url = f"{base_url}/order?meja={table_no.upper()}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="QR-{table_no}.png"'},
    )


# ================================================================
# API — MENU
# ================================================================

@app.get("/api/menu")
async def api_menu(category: Optional[str] = None):
    menus = db.get_menu()
    if category:
        menus = [m for m in menus if m.get("category", "") == category]
    return JSONResponse([
        {
            "name": m["name"],
            "price": m["price"],
            "category": m.get("category", ""),
            "emoji": m.get("emoji", ""),
        }
        for m in menus
    ])


# ================================================================
# API — QUEUE
# ================================================================

@app.get("/api/queue")
async def api_queue(status: Optional[str] = None):
    if status:
        statuses = [status]
    else:
        statuses = ["pending", "cooking"]
    return JSONResponse(db.get_queue(statuses))


class StatusUpdate(BaseModel):
    status: str


@app.post("/api/queue/{order_id}/status")
async def api_update_status(order_id: int, body: StatusUpdate):
    valid = {"pending", "cooking", "done", "cancelled"}
    if body.status not in valid:
        raise HTTPException(400, f"Status harus salah satu dari: {valid}")
    ok = db.update_status(order_id, body.status)
    if not ok:
        raise HTTPException(404, "Order tidak ditemukan")
    return {"ok": True}


# ================================================================
# API — CREATE ORDER (dari POS atau self-order QR)
# ================================================================

class OrderItem(BaseModel):
    name: str
    price: int
    qty: int = 1
    emoji: str = ""

class CreateOrder(BaseModel):
    items: List[OrderItem]
    customer_name: str
    table_no: str = ""
    note: str = ""
    source: str = "pos"


@app.post("/api/pos/order")
async def api_create_order(body: CreateOrder):
    if not body.items:
        raise HTTPException(400, "Items tidak boleh kosong")
    if not body.customer_name.strip():
        raise HTTPException(400, "Nama customer harus diisi")

    # Pydantic v1 (.dict()) & v2 (.model_dump()) -- dukung dua-duanya
    def _to_dict(item):
        return item.model_dump() if hasattr(item, "model_dump") else item.dict()

    items = [_to_dict(i) for i in body.items]
    order_id = db.create_order(
        items=items,
        customer_name=body.customer_name.strip(),
        table_no=body.table_no.upper().strip(),
        source=body.source,
        note=body.note.strip(),
    )
    return {"ok": True, "id": order_id}


# ================================================================
# API — STATS
# ================================================================

@app.get("/api/stats")
async def api_stats(tanggal: Optional[str] = None):
    return JSONResponse(db.stats_today(tanggal))


# ================================================================
# API — TABLES
# ================================================================

@app.get("/api/tables")
async def api_tables():
    return JSONResponse(db.get_tables())


class TableCreate(BaseModel):
    table_no: str
    name: str = ""
    capacity: int = 4


@app.post("/api/tables")
async def api_add_table(body: TableCreate):
    if not body.table_no.strip():
        raise HTTPException(400, "Nomor meja harus diisi")
    ok = db.add_table(body.table_no, body.name, body.capacity)
    if not ok:
        raise HTTPException(409, f"Meja {body.table_no} sudah ada")
    return {"ok": True}


@app.delete("/api/tables/{table_id}")
async def api_delete_table(table_id: int):
    ok = db.delete_table(table_id)
    if not ok:
        raise HTTPException(404, "Meja tidak ditemukan")
    return {"ok": True}


# ================================================================
# STARTUP (jalan di background thread)
# ================================================================

def start_pos_server() -> int:
    """Jalankan FastAPI POS di background thread. Return port aktif."""
    port_str = os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "8080")
    try:
        port = int(port_str)
    except ValueError:
        return 0

    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _run():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return port
