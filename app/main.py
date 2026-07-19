import os, sys, json, threading, time
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.config import HOST, PORT, TOTAL_TRAFFIC_LIMIT_GB, THEMES, STATIC_DIR, TEMPLATES_DIR, SUB_SOURCE_URL
from app.models import init_db, get_db, User, Client, SubscriptionConfig, Setting, TrafficStats, hash_password, verify_password
from app.auth import set_session, clear_session, get_current_user, authenticate_user, get_user, change_credentials, change_theme, get_user_theme
from app.xray import ensure_xray, start_xray, stop_xray, is_xray_running, get_system_stats, get_xray_status
from app.sub_manager import refresh_configs, get_all_configs, generate_qr_svg, generate_subscription_url, generate_clash_config, generate_singbox_config, get_progress

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[panel] Starting Spider Panel on {HOST}:{PORT}")
    init_db()
    threading.Thread(target=_startup_xray, daemon=True).start()
    yield
    stop_xray()

def _startup_xray():
    try:
        if ensure_xray():
            start_xray()
    except Exception as e:
        print(f"[panel] Xray startup note: {e}")

app = FastAPI(title="Spider Panel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def _auth_user(request: Request):
    user = get_current_user(request)
    return user

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _auth_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    db = next(get_db())
    try:
        if authenticate_user(db, username, password):
            response = RedirectResponse(url="/", status_code=302)
            set_session(response, username)
            return response
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"}, status_code=401)
    finally:
        db.close()

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    clear_session(response)
    return response

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = _auth_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    db = next(get_db())
    try:
        theme = get_user_theme(db, user)
        stats = get_system_stats()
        total_clients = db.query(Client).count()
        active_clients = db.query(Client).filter(Client.is_active == True).count()
        total_used = sum(c.traffic_used_gb for c in db.query(Client).all() if c.traffic_used_gb)
        total_used_gb = round(total_used, 2)
        return templates.TemplateResponse("dashboard.html", {
            "request": request, "username": user, "theme": theme,
            "cpu": stats["cpu_percent"], "ram_percent": stats["ram_percent"],
            "ram_used": stats["ram_used_mb"], "ram_total": stats["ram_total_mb"],
            "disk_percent": stats["disk_percent"], "disk_used": stats["disk_used_gb"],
            "disk_total": stats["disk_total_gb"], "active_users": active_clients,
            "total_clients": total_clients, "total_traffic_used": total_used_gb,
            "total_traffic_limit": TOTAL_TRAFFIC_LIMIT_GB,
            "traffic_percent": min(100, round((total_used_gb / max(TOTAL_TRAFFIC_LIMIT_GB, 1)) * 100, 1)),
            "xray_running": is_xray_running(),
        })
    finally:
        db.close()

@app.get("/api/stats")
async def api_stats(request: Request):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    stats = get_system_stats()
    db = next(get_db())
    try:
        total_used = sum(c.traffic_used_gb for c in db.query(Client).all() if c.traffic_used_gb)
        total_used_gb = round(total_used, 2)
        active_clients = db.query(Client).filter(Client.is_active == True).count()
        return JSONResponse({
            **stats, "active_users": active_clients,
            "total_traffic_used": total_used_gb,
            "total_traffic_limit": TOTAL_TRAFFIC_LIMIT_GB,
            "traffic_percent": min(100, round((total_used_gb / max(TOTAL_TRAFFIC_LIMIT_GB, 1)) * 100, 1)),
            "xray_running": is_xray_running(),
        })
    finally:
        db.close()

@app.get("/clients", response_class=HTMLResponse)
async def clients_page(request: Request):
    user = _auth_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    db = next(get_db())
    try:
        theme = get_user_theme(db, user)
        clients = db.query(Client).order_by(Client.id).all()
        client_list = [{"id": c.id, "username": c.username, "traffic_limit_gb": c.traffic_limit_gb,
                        "traffic_used_gb": round(c.traffic_used_gb, 3),
                        "traffic_percent": round(c.traffic_percent, 1),
                        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                        "expires_in_days": c.expires_in_days,
                        "is_active": c.is_active, "is_expired": c.is_expired or False} for c in clients]
        return templates.TemplateResponse("clients.html", {"request": request, "username": user, "theme": theme, "clients": client_list})
    finally:
        db.close()

@app.post("/api/clients/add")
async def add_client(request: Request, username: str = Form(...), traffic_limit_gb: float = Form(0.0), expires_days: int = Form(0)):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        existing = db.query(Client).filter(Client.username == username).first()
        if existing:
            return JSONResponse({"error": "Username already exists"}, status_code=400)
        expires_at = None
        if expires_days > 0:
            expires_at = datetime.utcnow().replace(hour=23, minute=59, second=59) + timedelta(days=expires_days)
        client = Client(username=username, traffic_limit_gb=traffic_limit_gb, traffic_used_gb=0.0,
                        expires_at=expires_at, is_active=True)
        db.add(client)
        db.commit()
        return JSONResponse({"success": True, "id": client.id})
    finally:
        db.close()

@app.post("/api/clients/delete/{client_id}")
async def delete_client(request: Request, client_id: int):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        db.delete(client)
        db.commit()
        return JSONResponse({"success": True})
    finally:
        db.close()

@app.post("/api/clients/toggle/{client_id}")
async def toggle_client(request: Request, client_id: int):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        client.is_active = not client.is_active
        db.commit()
        return JSONResponse({"success": True, "is_active": client.is_active})
    finally:
        db.close()

@app.post("/api/clients/traffic/{client_id}")
async def update_traffic(request: Request, client_id: int, amount_gb: float = Form(0.0)):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        client.traffic_used_gb += amount_gb
        if client.traffic_limit_gb > 0 and client.traffic_used_gb >= client.traffic_limit_gb:
            client.is_active = False
        db.commit()
        return JSONResponse({"success": True, "used": round(client.traffic_used_gb, 3)})
    finally:
        db.close()

@app.get("/subscription", response_class=HTMLResponse)
async def subscription_page(request: Request):
    user = _auth_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    db = next(get_db())
    try:
        theme = get_user_theme(db, user)
        configs = get_all_configs(db)
        total_used = sum(c.traffic_used_gb for c in db.query(Client).all() if c.traffic_used_gb)
        remaining_gb = max(0, TOTAL_TRAFFIC_LIMIT_GB - total_used)
        return templates.TemplateResponse("subscription.html", {
            "request": request, "username": user, "theme": theme, "configs": configs,
            "remaining_traffic": round(remaining_gb, 2), "total_traffic_limit": TOTAL_TRAFFIC_LIMIT_GB,
            "traffic_percent": min(100, round((total_used / max(TOTAL_TRAFFIC_LIMIT_GB, 1)) * 100, 1)),
        })
    finally:
        db.close()

@app.post("/api/subscription/refresh")
async def refresh_subscription(request: Request):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        result = refresh_configs(db)
        return JSONResponse(result)
    finally:
        db.close()

@app.get("/api/subscription/progress")
async def sub_progress(request: Request):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(get_progress())

@app.get("/api/configs")
async def get_configs(request: Request):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        return JSONResponse(get_all_configs(db))
    finally:
        db.close()

@app.get("/api/qr/{config_id}")
async def get_qr(request: Request, config_id: int):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        cfg = db.query(SubscriptionConfig).filter(SubscriptionConfig.id == config_id).first()
        if not cfg:
            return JSONResponse({"error": "Not found"}, status_code=404)
        svg = generate_qr_svg(cfg.config_data)
        return Response(content=svg, media_type="image/svg+xml")
    finally:
        db.close()

@app.get("/sub/{format}")
async def export_sub(request: Request, format: str):
    db = next(get_db())
    try:
        if format == "base64":
            content = generate_subscription_url(db, "base64")
            return PlainTextResponse(content)
        elif format == "v2ray":
            content = generate_subscription_url(db, "v2ray")
            return PlainTextResponse(content)
        elif format == "clash":
            content = generate_clash_config(db)
            return Response(content=content, media_type="text/yaml")
        elif format == "singbox":
            content = generate_singbox_config(db)
            return Response(content=content, media_type="application/json")
        else:
            return JSONResponse({"error": "Unknown format"}, status_code=400)
    finally:
        db.close()

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = _auth_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    db = next(get_db())
    try:
        theme = get_user_theme(db, user)
        current_user = db.query(User).filter(User.username == user).first()
        return templates.TemplateResponse("settings.html", {
            "request": request, "username": user, "theme": theme,
            "current_username": current_user.username if current_user else user,
        })
    finally:
        db.close()

@app.post("/api/settings")
async def save_settings(request: Request, new_username: str = Form(...), current_password: str = Form(""), new_password: str = Form(""), theme: str = Form("blue")):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        if not authenticate_user(db, user, current_password):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=403)
        msg = change_credentials(db, user, new_username if new_username != user else None, new_password if new_password else None)
        if "error" in msg.lower() or "taken" in msg.lower() or "not found" in msg.lower():
            return JSONResponse({"error": msg}, status_code=400)
        if theme in THEMES:
            target_user = new_username if new_username and new_username != user else user
            change_theme(db, target_user, theme)
        response_data = {"success": True, "message": msg, "username_changed": False}
        if new_username and new_username != user:
            response_data["username_changed"] = True
            response_data["new_username"] = new_username
        return JSONResponse(response_data)
    finally:
        db.close()

@app.post("/api/theme")
async def api_change_theme(request: Request, theme: str = Form(...)):
    user = _auth_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = next(get_db())
    try:
        if theme in THEMES:
            change_theme(db, user, theme)
            return JSONResponse({"success": True})
        return JSONResponse({"error": "Invalid theme"}, status_code=400)
    finally:
        db.close()

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "xray": is_xray_running()})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")