"""
keepalive.py

Server HTTP kecil supaya Render Free Web Service:
1. Mendeteksi ada port terbuka (syarat Render supaya deploy dianggap sukses).
2. Bisa di-ping berkala oleh UptimeRobot supaya service tidak "tidur"
   setelah 15 menit tanpa trafik.

Routes:
  GET /              -> plain text "bot is running" (untuk UptimeRobot)
  GET /dashboard     -> HTML dashboard order hari ini (admin)
  GET /dashboard?tanggal=YYYY-MM-DD -> order tanggal tertentu
  GET /api/orders    -> JSON data order hari ini

Keamanan: kalau DASHBOARD_TOKEN diisi di .env, tiap request ke /dashboard
dan /api/orders harus menyertakan ?token=... yang cocok.
Kalau tidak diisi, dashboard terbuka (aman untuk intranet/lokal).
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

# Diisi dari bot.py setelah adapter siap, supaya dashboard bisa akses DB.
_order_store = None


def set_order_store(store) -> None:
    global _order_store
    _order_store = store


def _dashboard_token() -> str:
    return os.getenv("DASHBOARD_TOKEN", "").strip()


def _check_token(query_params: dict) -> bool:
    required = _dashboard_token()
    if not required:
        return True
    given = query_params.get("token", [""])[0]
    return given == required


def _html_page(title: str, body: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:sans-serif;background:#f5f5f5;color:#222;padding:16px}}
h1{{font-size:1.4rem;margin-bottom:4px}}
.sub{{color:#666;font-size:.85rem;margin-bottom:20px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}
.card{{background:#fff;border-radius:8px;padding:16px 20px;min-width:140px;
       box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card .num{{font-size:1.6rem;font-weight:700;color:#1a73e8}}
.card .lbl{{font-size:.8rem;color:#666;margin-top:2px}}
table{{width:100%;border-collapse:collapse;background:#fff;
       border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th{{background:#1a73e8;color:#fff;text-align:left;padding:10px 12px;
    font-size:.82rem;font-weight:600}}
td{{padding:9px 12px;font-size:.85rem;border-bottom:1px solid #eee;
    vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#f0f7ff}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75rem}}
.ok{{background:#e6f4ea;color:#1e8e3e}}
.pending{{background:#fef3cd;color:#856404}}
.empty{{text-align:center;padding:40px;color:#999}}
.nav{{margin-bottom:16px;font-size:.85rem}}
.nav a{{color:#1a73e8;text-decoration:none;margin-right:12px}}
.nav a:hover{{text-decoration:underline}}
h2{{font-size:1rem;margin:24px 0 10px;color:#444}}
</style>
</head>
<body>
{body}
<p style="margin-top:24px;font-size:.75rem;color:#aaa">
  Auto-refresh setiap 30 detik &bull; Madam Lily Bot
</p>
</body>
</html>"""
    return html.encode("utf-8")


def _build_dashboard(date_str: str, query_params: dict) -> bytes:
    from datetime import date, timedelta

    token_suffix = ""
    tok = _dashboard_token()
    if tok:
        token_suffix = f"&token={tok}"

    today_str = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    nav = (
        f'<div class="nav">'
        f'<a href="/dashboard?tanggal={yesterday_str}{token_suffix}">&#8592; Kemarin</a>'
        f'<a href="/dashboard?tanggal={today_str}{token_suffix}">Hari Ini</a>'
        f'<a href="/dashboard?tanggal={tomorrow_str}{token_suffix}">Besok &#8594;</a>'
        f'</div>'
    )

    if _order_store is None:
        body = f"<h1>Dashboard Madam Lily</h1>{nav}<p class='empty'>Database belum tersedia.</p>"
        return _html_page("Dashboard - Madam Lily", body)

    summary = _order_store.daily_summary(date_str)
    orders = _order_store.orders_by_date(date_str)

    order_count = summary["order_count"]
    total_riel = summary["total_riel"]
    total_usd = summary["total_usd"]

    label = "Hari Ini" if date_str == today_str else date_str
    cards = (
        f'<div class="cards">'
        f'<div class="card"><div class="num">{order_count}</div>'
        f'<div class="lbl">Order {label}</div></div>'
        f'<div class="card"><div class="num">{total_riel:,}</div>'
        f'<div class="lbl">Total Riel</div></div>'
        f'<div class="card"><div class="num">${total_usd:,.2f}</div>'
        f'<div class="lbl">Total USD</div></div>'
        f'</div>'
    )

    if not orders:
        table = "<p class='empty'>Belum ada order untuk tanggal ini.</p>"
    else:
        rows_html = ""
        for o in orders:
            jam = o["created_at"][11:16] if len(o.get("created_at", "")) >= 16 else "-"
            dest = o.get("destination") or "-"
            pemesan = o.get("orderer_name") or "-"
            riel = f"{o['grand_total_riel']:,}"
            usd = f"${o['grand_total_usd']:,.2f}"

            # Ambil nama-nama customer dari payload
            customers = "-"
            try:
                payload = json.loads(o["payload"])
                names = [
                    inv["telegram_name"]
                    for inv in payload.get("invoices", [])
                    if inv.get("telegram_name")
                ]
                if names:
                    customers = ", ".join(names[:5])
                    if len(names) > 5:
                        customers += f" +{len(names)-5}"
            except Exception:
                pass

            rows_html += (
                f"<tr>"
                f"<td>{jam}</td>"
                f"<td>{customers}</td>"
                f"<td>{riel}<br><small style='color:#888'>{usd}</small></td>"
                f"<td>{dest}</td>"
                f"<td>{pemesan}</td>"
                f"</tr>"
            )

        table = (
            f'<table>'
            f'<thead><tr>'
            f'<th>Jam</th><th>Customer</th>'
            f'<th>Total</th><th>Lokasi</th><th>Pemesan</th>'
            f'</tr></thead>'
            f'<tbody>{rows_html}</tbody>'
            f'</table>'
        )

    # Pre-order pending
    preorder_html = ""
    try:
        pending = _order_store.upcoming_preorders(today_str)
        if pending:
            po_rows = ""
            for po in pending:
                preview = po["order_text"][:60].replace("\n", " / ")
                if len(po["order_text"]) > 60:
                    preview += "..."
                po_rows += (
                    f"<tr>"
                    f"<td>#{po['id']}</td>"
                    f"<td>{po['scheduled_date']}</td>"
                    f"<td>{po['chat_id']}</td>"
                    f"<td>{preview}</td>"
                    f'<td><span class="badge pending">Pending</span></td>'
                    f"</tr>"
                )
            preorder_html = (
                f'<h2>📅 Pre-order Pending</h2>'
                f'<table>'
                f'<thead><tr><th>#</th><th>Jadwal</th>'
                f'<th>Chat ID</th><th>Preview</th><th>Status</th></tr></thead>'
                f'<tbody>{po_rows}</tbody></table>'
            )
    except Exception:
        pass

    body = (
        f"<h1>📊 Dashboard Madam Lily</h1>"
        f'<p class="sub">Tanggal: {date_str} &bull; '
        f'<a href="/dashboard?tanggal={date_str}{token_suffix}" '
        f'style="color:#1a73e8;font-size:.8rem">&#8635; Refresh</a></p>'
        f"{nav}{cards}{table}{preorder_html}"
    )
    return _html_page(f"Dashboard {date_str} - Madam Lily", body)


def _build_orders_json(date_str: str) -> bytes:
    if _order_store is None:
        return json.dumps({"error": "store not ready"}).encode()

    summary = _order_store.daily_summary(date_str)
    orders = _order_store.orders_by_date(date_str)

    # Tidak kirim payload penuh ke JSON API (bisa besar)
    slim = [
        {
            "id": o["id"],
            "created_at": o["created_at"],
            "destination": o["destination"],
            "orderer_name": o["orderer_name"],
            "grand_total_riel": o["grand_total_riel"],
            "grand_total_usd": o["grand_total_usd"],
        }
        for o in orders
    ]
    return json.dumps(
        {"date": date_str, "summary": dict(summary), "orders": slim},
        ensure_ascii=False,
    ).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/dashboard",):
            if not _check_token(params):
                self._respond(403, "text/plain", b"403 Forbidden: token salah")
                return
            from datetime import date
            date_str = params.get("tanggal", [date.today().isoformat()])[0]
            self._respond(200, "text/html; charset=utf-8", _build_dashboard(date_str, params))

        elif path in ("/api/orders",):
            if not _check_token(params):
                self._respond(403, "application/json", b'{"error":"forbidden"}')
                return
            from datetime import date
            date_str = params.get("tanggal", [date.today().isoformat()])[0]
            self._respond(200, "application/json; charset=utf-8", _build_orders_json(date_str))

        else:
            self._respond(200, "text/plain", b"Madam Lily Bot is running")

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_keepalive_server() -> int:
    """Start HTTP server. Return port yang dipakai, atau 0 kalau gagal.

    Prioritas port:
      1. PORT  (diset otomatis oleh Render / hosting lain)
      2. DASHBOARD_PORT  (diset manual, untuk lokal / VPS tanpa PORT)
      3. 8080  (default lokal)
    """
    port_str = os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "8080")

    try:
        port = int(port_str)
    except ValueError:
        return 0

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
    except OSError as e:
        # Port sudah dipakai -- jangan crash bot utama
        import sys
        print(f"[keepalive] Tidak bisa buka port {port}: {e}", file=sys.stderr)
        return 0

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port
