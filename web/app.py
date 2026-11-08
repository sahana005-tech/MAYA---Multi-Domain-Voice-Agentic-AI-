import os, json, sqlite3, hashlib, secrets
from contextlib import contextmanager
from fastapi import FastAPI, Request, Form, HTTPException, Depends, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = os.path.dirname(__file__)
DB_PATH = os.environ.get("VOICE_AGENT_DB",
                         os.path.join(os.path.dirname(BASE), "src", "voice_agent.db"))
app = FastAPI(title="Voice Agent Console")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

def _read_template(name, **ctx):
    with open(os.path.join(BASE, "templates", name), encoding="utf-8") as fh:
        html = fh.read()
    for k, v in ctx.items():
        html = html.replace("{{ " + k + " }}", str(v))
    return HTMLResponse(html)
SESSIONS = {}
PENDING_SIGNUPS = {}   # email -> {name, mobile, password_hash, code, expires}

import sys, time, secrets as _secrets
sys.path.insert(0, os.path.join(os.path.dirname(BASE), "src"))

def _gen_otp():
    return f"{_secrets.randbelow(1000000):06d}"

def _send_otp_email(to_email, code, name):
    try:
        import messaging
        subject = "Your verification code"
        body = f"Hi {name},\n\nYour verification code is: {code}\n\nIt expires in 10 minutes.\n\n- Multi-Domain Voice Agentic AI"
        ok, info = messaging.send_email(subject, body, to=to_email)
        return ok
    except Exception as e:
        print("OTP email failed:", e)
        return False

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn; conn.commit()
    finally:
        conn.close()

def init_users_table():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                email TEXT UNIQUE NOT NULL, mobile TEXT, password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, domain_id TEXT, status TEXT DEFAULT 'active',
                task_completed INTEGER DEFAULT 0, tools_called TEXT DEFAULT '[]',
                rating INTEGER, transcript TEXT DEFAULT '',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ended_at TIMESTAMP);
            CREATE TABLE IF NOT EXISTS bookings (
                ref_id TEXT PRIMARY KEY, call_id TEXT, domain_id TEXT, service_id TEXT,
                datetime TEXT, quantity INTEGER, customer TEXT, total REAL,
                status TEXT DEFAULT 'confirmed', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        """)

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"{salt}${hashlib.sha256((salt+password).encode()).hexdigest()}"

def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
        return hash_password(password, salt) == stored
    except Exception:
        return False

def current_user(session: str = Cookie(default=None)):
    return SESSIONS.get(session) if session else None

@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return _read_template("signup.html")

@app.post("/signup")
def signup(name: str = Form(...), email: str = Form(...), mobile: str = Form(...), password: str = Form(...)):
    with get_db() as c:
        if c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            return RedirectResponse("/signup?error=exists", status_code=303)
    code = _gen_otp()
    PENDING_SIGNUPS[email] = {"name": name, "mobile": mobile,
                             "password_hash": hash_password(password),
                             "code": code, "expires": time.time() + 600}
    sent = _send_otp_email(email, code, name)
    if not sent:
        print(f"[OTP fallback] code for {email} is {code}")
    resp = RedirectResponse(f"/verify?email={email}", status_code=303)
    return resp

@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, email: str = "", error: str = ""):
    html = _read_template("verify.html")
    return HTMLResponse(html.body.decode().replace("{{ email }}", email))

@app.post("/verify")
def verify_code(email: str = Form(...), code: str = Form(...)):
    p = PENDING_SIGNUPS.get(email)
    if not p:
        return RedirectResponse("/signup", status_code=303)
    if time.time() > p["expires"]:
        del PENDING_SIGNUPS[email]
        return RedirectResponse(f"/verify?email={email}&error=expired", status_code=303)
    if code.strip() != p["code"]:
        return RedirectResponse(f"/verify?email={email}&error=wrong", status_code=303)
    with get_db() as c:
        c.execute("INSERT INTO users (name,email,mobile,password_hash) VALUES (?,?,?,?)",
                  (p["name"], email, p["mobile"], p["password_hash"]))
    del PENDING_SIGNUPS[email]
    tok = secrets.token_urlsafe(24); SESSIONS[tok] = email
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", tok, httponly=True, max_age=86400)
    return resp

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _read_template("login.html")

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    with get_db() as c:
        user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=invalid", status_code=303)
    token = secrets.token_urlsafe(24); SESSIONS[token] = email
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, max_age=86400)
    return resp

@app.get("/logout")
def logout(session: str = Cookie(default=None)):
    SESSIONS.pop(session, None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    with get_db() as c:
        u = c.execute("SELECT name FROM users WHERE email=?", (user,)).fetchone()
    return _read_template("dashboard.html", user_name=(u["name"] if u else user))

@app.get("/api/metrics")
def api_metrics(user=Depends(current_user)):
    if not user: raise HTTPException(401)
    with get_db() as c:
        active = c.execute("SELECT COUNT(*) n FROM calls WHERE status='active'").fetchone()["n"]
        total = c.execute("SELECT COUNT(*) n FROM calls WHERE status='ended'").fetchone()["n"]
        completed = c.execute("SELECT COUNT(*) n FROM calls WHERE task_completed=1").fetchone()["n"]
        avg = c.execute("SELECT AVG(rating) a FROM calls WHERE rating IS NOT NULL").fetchone()["a"]
        domains = c.execute("SELECT domain_id, COUNT(*) n FROM calls GROUP BY domain_id ORDER BY n DESC").fetchall()
        bookings = c.execute("SELECT COUNT(*) n FROM bookings").fetchone()["n"]
    return {"active_calls": active, "total_conversations": total, "tasks_completed": completed,
            "avg_rating": round(avg,2) if avg is not None else None, "total_bookings": bookings,
            "domains": [{"domain": d["domain_id"] or "unknown", "count": d["n"]} for d in domains]}

@app.get("/api/conversations")
def api_conversations(user=Depends(current_user)):
    if not user: raise HTTPException(401)
    with get_db() as c:
        rows = c.execute("""SELECT id,domain_id,status,task_completed,rating,tools_called
                            FROM calls ORDER BY started_at DESC LIMIT 15""").fetchall()
    out = []
    for r in rows:
        try: tools = json.loads(r["tools_called"] or "[]")
        except Exception: tools = []
        out.append({"id": r["id"], "domain": r["domain_id"] or "unknown", "status": r["status"],
                    "completed": bool(r["task_completed"]), "rating": r["rating"], "tools": tools})
    return {"conversations": out}

@app.get("/api/active-domain")
def api_active_domain(user=Depends(current_user)):
    if not user: raise HTTPException(401)
    root = os.path.dirname(BASE)
    try:
        with open(os.path.join(root, "active_domain.txt")) as fh:
            return {"active": fh.read().strip() or "restaurant"}
    except FileNotFoundError:
        return {"active": "restaurant"}

@app.post("/api/switch-domain")
def api_switch_domain(domain: str = Form(...), user=Depends(current_user)):
    if not user: raise HTTPException(401)
    allowed = {"restaurant","hospital","hotel","flight"}
    if domain not in allowed:
        raise HTTPException(400, "unknown domain")
    root = os.path.dirname(BASE)
    with open(os.path.join(root, "active_domain.txt"), "w") as fh:
        fh.write(domain)
    return {"active": domain, "note": "restart the voice agent to apply"}


@app.on_event("startup")
def _startup():
    init_users_table()
