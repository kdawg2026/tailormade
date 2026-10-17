"""Tailormade web app — FastAPI, deployable to Vercel as one Python function.

  uvicorn app:app --port 8000      # local; reads .env.local from `vercel env pull`

Accounts get FREE_TAILORS free tailors, then spend subscription allowance or
pack credits. A credit is reserved before the model is called and refunded if
no attempt passes every check, so users pay only for finished documents.

Import and tailor take minutes, so they answer with a stream of JSON lines:
  {"log": "..."}            progress, shown as it arrives
  {"result": {...}}         import finished
  {"files": [...]}          tailor finished; each file is base64
  {"error": {...}}          failed; header, items, advice
Blank lines are heartbeats that keep idle proxies from closing the stream.

API (JSON):
  POST /api/signup, /api/login {"email", "password"}   POST /api/logout
  GET  /api/me                                          -> balance
  POST /api/import {"filename", "data": base64}          (stream)
  POST /api/tailor {"master", "jd", "company", "rewrite", "approve"} (stream)
  POST /api/checkout {"plan"} -> {"url"}                POST /api/portal -> {"url"}
  POST /api/subscription/cancel, /api/subscription/resume
  POST /api/password {"current", "new"}                 POST /api/account/delete {"password"}
  POST /api/stripe/webhook                              (Stripe-signed)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import shutil
import tempfile
import threading
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import auth
import billing
import db
from resume_import import ResumeImportError, import_resume
from tailormade import TailorError, parse_word_list, tailor

ROOT = Path(__file__).parent
WEB = ROOT / "web"
CERT_SKILLS = json.loads((ROOT / "cert_skills.json").read_text(encoding="utf-8"))
PAGES = {
    "/": (WEB / "index.html", "text/html; charset=utf-8"),
    "/docs": (WEB / "docs.html", "text/html; charset=utf-8"),
    "/account": (WEB / "account.html", "text/html; charset=utf-8"),
    "/account.js": (WEB / "account.js", "text/javascript; charset=utf-8"),
    "/style.css": (WEB / "style.css", "text/css; charset=utf-8"),
    "/app.js": (WEB / "app.js", "text/javascript; charset=utf-8"),
}
# Vercel's request body limit is 4.5 MB; base64 adds a third, so ~3 MB files.
MAX_BODY_BYTES = 4 * 1024 * 1024
HEARTBEAT_SECONDS = 15
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
SECURITY_HEADERS = {
    # Only this site's own files may run, and no other site may frame it
    # (clickjacking the Tee off button to spend someone's credits).
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def allowed_hosts() -> set[str]:
    """This deployment's own hostnames. Custom domains go in APP_HOSTS."""
    hosts = set(LOCAL_HOSTS)
    for name in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        if os.environ.get(name):
            hosts.add(os.environ[name])
    hosts.update(h.strip() for h in os.environ.get("APP_HOSTS", "").split(",") if h.strip())
    return hosts


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Refuse requests another website could make through the browser.

    The Host check defeats DNS rebinding. On POST, a same-host Origin, a
    same-origin Sec-Fetch-Site, and a JSON content type (which forces a CORS
    preflight this app never approves) together stop cross-site form posts.
    Stripe's webhook is exempt: it has no browser origin and is verified by
    its signature instead.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in allowed_hosts():
        response = error(403, "Unknown host.")
    elif request.method == "POST" and request.url.path != "/api/stripe/webhook" and (
        (request.headers.get("origin") is not None
         and urlsplit(request.headers["origin"]).hostname != host)
        or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none")
        or not request.headers.get("content-type", "").startswith("application/json")
    ):
        response = error(403, "Cross-site request refused.")
    elif not (request.headers.get("content-length") or "0").isdigit():
        response = error(400, "Invalid Content-Length.")
    elif int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        response = error(413, "Upload is over 3 MB.")
    else:
        response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def base_url(request: Request) -> str:
    host = request.headers["host"]
    scheme = "http" if host.rsplit(":", 1)[0] in LOCAL_HOSTS else "https"
    return f"{scheme}://{host}"


def client_ip(request: Request) -> str:
    # Vercel sets x-forwarded-for itself; its first entry is the real client.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> dict | None:
    return auth.user_for_session(request.cookies.get(auth.SESSION_COOKIE))


async def read_json(request: Request) -> dict | None:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:  # chunked bodies carry no Content-Length
        return None
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def with_session(response: JSONResponse, token: str, request: Request) -> JSONResponse:
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return response


# ---- pages -----------------------------------------------------------------

def _page_route(file: Path, media_type: str):
    def page():
        return FileResponse(file, media_type=media_type)
    return page


for _path, (_file, _type) in PAGES.items():
    app.get(_path, include_in_schema=False)(_page_route(_file, _type))


# ---- accounts --------------------------------------------------------------

@app.post("/api/signup")
async def signup(request: Request):
    body = await read_json(request) or {}
    try:
        token = auth.signup(str(body.get("email", "")), str(body.get("password", "")),
                            client_ip(request))
    except auth.AuthError as exc:
        return error(400, str(exc))
    user = auth.user_for_session(token)
    return with_session(JSONResponse(db.balance(user)), token, request)


@app.post("/api/login")
async def login(request: Request):
    body = await read_json(request) or {}
    try:
        token = auth.login(str(body.get("email", "")), str(body.get("password", "")))
    except auth.AuthError as exc:
        return error(400, str(exc))
    user = auth.user_for_session(token)
    return with_session(JSONResponse(db.balance(user)), token, request)


@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.logout(token)
    response = JSONResponse({})
    response.delete_cookie(auth.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    return db.balance(user)


# ---- streamed work ---------------------------------------------------------

def stream(work):
    """Run work(log) on a thread and stream its log, then its outcome."""
    lines: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            lines.put(work(lambda text: lines.put({"log": text})))
        except TailorError as exc:
            f = exc.failure
            lines.put({"error": {"header": f.header, "items": f.items, "advice": f.advice}})
        except (ResumeImportError, RuntimeError, ValueError) as exc:
            lines.put({"error": {"header": str(exc), "items": [], "advice": ""}})
        except Exception:  # surface anything else instead of a silent dead stream
            traceback.print_exc()
            lines.put({"error": {"header": "Unexpected server error. Try again.",
                                 "items": [], "advice": ""}})

    threading.Thread(target=run, daemon=True).start()

    def body():
        while True:
            try:
                item = lines.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield "\n"
                continue
            yield json.dumps(item) + "\n"
            if "log" not in item:
                return

    return StreamingResponse(body(), media_type="application/x-ndjson")


@app.post("/api/import")
async def import_route(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    body = await read_json(request) or {}
    filename = str(body.get("filename") or "")
    try:
        data = base64.b64decode(str(body.get("data") or ""), validate=True)
    except binascii.Error:
        return error(400, "File data is not base64.")
    if not filename or not data:
        return error(400, "No file uploaded.")
    # JSON is parsed locally with no model call, so it costs nothing to allow.
    usage_id = None
    if Path(filename).suffix.lower() != ".json":
        usage_id = db.record_import(user["id"])
        if usage_id is None:
            return error(429, f"You've reached {db.IMPORTS_PER_DAY} resume imports today. Try again tomorrow.")

    def work(log) -> dict:
        master, warnings = import_resume(
            filename, data, log=log,
            on_model_call=lambda call: db.record_api_call(user["id"], usage_id, "import", call),
        )
        return {"result": {"master": master, "warnings": warnings}}

    return stream(work)


@app.post("/api/tailor")
async def tailor_route(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    body = await read_json(request) or {}
    master, jd = body.get("master"), str(body.get("jd") or "")
    if not isinstance(master, dict):
        return error(400, "Resume JSON is missing.")
    if not jd.strip():
        return error(400, "Job description is empty.")
    usage_id = db.reserve_tailor(user["id"])
    if usage_id is None:
        return error(402, "You're out of tailors. Buy a pack or start the monthly plan to keep going.")
    rewrite, approve = bool(body.get("rewrite")), str(body.get("approve") or "")
    company = str(body.get("company") or "")

    def work(log) -> dict:
        workdir = Path(tempfile.mkdtemp(prefix="tailormade-"))
        succeeded = False
        try:
            paths = tailor(master, CERT_SKILLS, jd, workdir, rewrite_bullets=rewrite,
                           approved=parse_word_list(approve), company=company, log=log,
                           on_model_call=lambda call: db.record_api_call(
                               user["id"], usage_id, "tailor", call))
            files = [{
                "name": p.name,
                "type": CONTENT_TYPES.get(p.suffix, "application/octet-stream"),
                "data": base64.b64encode(p.read_bytes()).decode("ascii"),
            } for p in paths]
            succeeded = True
            return {"files": files}
        finally:
            db.settle_tailor(usage_id, succeeded)
            shutil.rmtree(workdir, ignore_errors=True)

    return stream(work)


# ---- billing ---------------------------------------------------------------

@app.post("/api/checkout")
async def checkout(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    body = await read_json(request) or {}
    try:
        return {"url": billing.checkout_url(user, str(body.get("plan", "")), base_url(request))}
    except ValueError as exc:
        return error(400, str(exc))


@app.post("/api/portal")
async def portal(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    try:
        return {"url": billing.portal_url(user, base_url(request))}
    except ValueError as exc:
        return error(400, str(exc))


@app.post("/api/subscription/cancel")
@app.post("/api/subscription/resume")
async def subscription_cancel(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    try:
        billing.set_cancel_at_period_end(user, request.url.path.endswith("/cancel"))
    except ValueError as exc:
        return error(400, str(exc))
    return db.balance(current_user(request))


# ---- account settings ------------------------------------------------------

@app.post("/api/password")
async def change_password(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    body = await read_json(request) or {}
    try:
        auth.change_password(user["id"], str(body.get("current", "")), str(body.get("new", "")),
                             request.cookies[auth.SESSION_COOKIE])
    except auth.AuthError as exc:
        return error(400, str(exc))
    return {}


@app.post("/api/account/delete")
async def delete_account(request: Request):
    user = current_user(request)
    if user is None:
        return error(401, "Log in to continue.")
    body = await read_json(request) or {}
    try:
        auth.confirm_password(user["id"], str(body.get("password", "")))
    except auth.AuthError as exc:
        return error(400, str(exc))
    # Stripe first: if it fails, the account stays and the user can retry,
    # instead of leaving a subscription billing someone with no account.
    billing.delete_customer(user)
    auth.delete_user(user["id"])
    response = JSONResponse({})
    response.delete_cookie(auth.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    try:
        billing.handle_webhook(await request.body(), request.headers.get("stripe-signature"))
    except ValueError as exc:
        return error(400, str(exc))
    return {"received": True}
