# api/main.py - Tick.IT backend: QR tickets for any local business (Vercel ready)
#
# Model
#   owners   = business accounts (businessName, category, bizCode)
#   services = things an owner sells tickets for (Eco Park entry, Parking, Auto ride...)
#   users    = customers with a wallet
#   tickets  = a purchase of N admissions for one service; redeemed once by the owner
#
# Security
#   * Password accounts: werkzeug hash, 3 failed attempts -> 1 hour lockout.
#   * Google accounts: Firebase ID token verified server side, exchanged for an app JWT.
#   * Everything except register/login/Google and the read-only /api/public/* endpoints
#     needs "Authorization: Bearer <token>"; the acting user always comes from the token.
#   * Prices and totals are computed on the server. All timestamps are UTC.

import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from werkzeug.security import check_password_hash, generate_password_hash

import firebase_admin
from firebase_admin import auth, credentials, firestore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tickit")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MAX_FAILED_LOGINS = 3
LOCKOUT_DURATION = timedelta(hours=1)
TOKEN_LIFETIME = timedelta(hours=8)
WELCOME_BONUS = 500          # demo wallet balance for new customers
MAX_TOPUP = 10000            # demo "add funds" cap per request
MIN_PASSWORD_LENGTH = 8
MAX_QUANTITY = 10            # admissions per booking
MAX_PRICE = 100000
MIN_VALIDITY, MAX_VALIDITY = 5, 43200   # minutes (5 min .. 30 days)

# category -> ID prefix. IDs look like PRK-7K3QM2.
CATEGORIES = {
    "park": "PRK", "temple": "TMP", "parking": "PKG",
    "transport": "RDE", "event": "EVT", "other": "SVC",
}
ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
SERVICE_ID_RE = re.compile(r"^[A-Z]{3}-[A-Z0-9]{6}$")
BIZ_CODE_RE = re.compile(r"^[A-Z0-9]{6}$")
TICKET_CODE_RE = re.compile(r"^[A-Z0-9]{8}$")

IS_PRODUCTION = bool(os.environ.get("VERCEL") or os.environ.get("PRODUCTION"))
SECRET_KEY = os.environ.get("JWT_SECRET")
if not SECRET_KEY:
    if IS_PRODUCTION:
        log.error("JWT_SECRET is not set. Authentication endpoints are disabled.")
    else:
        SECRET_KEY = secrets.token_hex(32)
        log.warning("JWT_SECRET not set - using a random key (tokens reset on every restart).")

# --------------------------------------------------------------------------- #
# Firebase initialisation
# --------------------------------------------------------------------------- #
try:
    service_account_info_str = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if service_account_info_str:
        cred = credentials.Certificate(json.loads(service_account_info_str))
    else:
        cred = credentials.Certificate("serviceAccountKey.json")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    log.info("Firebase connection successful.")
except Exception as exc:  # pragma: no cover - depends on deployment
    log.error("Firebase connection failed: %s", exc)
    db = None

# --------------------------------------------------------------------------- #
# App, CORS, rate limiting, headers
# --------------------------------------------------------------------------- #
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

_allowed = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed:
    CORS(app, origins=_allowed, allow_headers=["Content-Type", "Authorization"])


def _client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


limiter = Limiter(
    _client_ip,
    app=app,
    default_limits=["300 per hour"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)


@app.errorhandler(429)
def _rate_limited(_e):
    return jsonify({"error": "Too many requests. Please slow down and try again shortly."}), 429


@app.errorhandler(404)
def _not_found(_e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def _bad_method(_e):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "Request too large"}), 413


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return resp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_utc():
    return datetime.now(timezone.utc)


def as_utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def iso(value):
    value = as_utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


_TAG_RE = re.compile(r"<[^>]*>")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value, max_len=80):
    """Strip markup/control characters from free text that is later rendered."""
    if not isinstance(value, str):
        return ""
    value = _CTRL_RE.sub("", _TAG_RE.sub("", value)).replace("<", "").replace(">", "")
    return " ".join(value.split())[:max_len]


def normalise_email(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if EMAIL_RE.match(value) else None


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def server_error(context, exc):
    log.exception("%s: %s", context, exc)
    return jsonify({"error": "Something went wrong. Please try again."}), 500


def require_db(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not db:
            return jsonify({"error": "Database not initialized"}), 500
        return f(*args, **kwargs)
    return wrapper


COLLECTION_FOR_ROLE = {"customer": "users", "owner": "owners"}


def issue_token(user_id, role):
    issued = now_utc()
    return jwt.encode(
        {"userId": user_id, "role": role, "iat": issued, "exp": issued + TOKEN_LIFETIME},
        SECRET_KEY, algorithm="HS256",
    )


def token_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not SECRET_KEY:
                return jsonify({"error": "Server is not configured"}), 500
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or not header.split(" ", 1)[1].strip():
                return jsonify({"error": "Missing or malformed authorization token"}), 401
            try:
                decoded = jwt.decode(header.split(" ", 1)[1].strip(), SECRET_KEY, algorithms=["HS256"])
                g.current_user_id = decoded["userId"]
                g.current_role = decoded["role"]
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Session expired. Please log in again."}), 401
            except Exception:
                return jsonify({"error": "Invalid authentication token"}), 401
            if roles and g.current_role not in roles:
                return jsonify({"error": "You do not have access to this resource"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_self(path_id):
    if path_id != g.current_user_id:
        return jsonify({"error": "You do not have access to this resource"}), 403
    return None


def random_code(length):
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(length))


def find_by_email(collection, email):
    for doc in db.collection(collection).where("email", "==", email).limit(1).stream():
        return doc
    return None


def new_service_id(category):
    prefix = CATEGORIES.get(category, "SVC")
    for _ in range(10):
        candidate = f"{prefix}-{random_code(6)}"
        if not db.collection("services").document(candidate).get().exists:
            return candidate
    raise RuntimeError("could not allocate a service id")


def new_biz_code():
    for _ in range(10):
        candidate = random_code(6)
        if not any(True for _d in db.collection("owners").where("bizCode", "==", candidate).limit(1).stream()):
            return candidate
    raise RuntimeError("could not allocate a business code")


def public_profile(data, role, doc_id):
    profile = {
        "uid": doc_id, "role": role,
        "fullName": data.get("fullName"), "email": data.get("email"),
        "authProvider": data.get("authProvider", "password"),
    }
    if role == "customer":
        profile["walletBalance"] = data.get("walletBalance", 0)
    else:
        profile["businessName"] = data.get("businessName")
    return profile


def service_view(doc_id, s, business_name=None):
    return {
        "serviceId": doc_id,
        "name": s.get("name"),
        "description": s.get("description", ""),
        "category": s.get("category", "other"),
        "price": int(s.get("price", 0)),
        "validityMinutes": int(s.get("validityMinutes", 60)),
        "active": bool(s.get("active", True)),
        "businessName": business_name if business_name is not None else s.get("businessName"),
        "createdAt": iso(s.get("createdAt")),
    }


def ticket_state(ticket, now):
    """VALID | USED | EXPIRED"""
    status = str(ticket.get("status", "VALID")).upper()
    if status == "USED":
        return "USED"
    expires = as_utc(ticket.get("expiresAt"))
    if status == "EXPIRED" or (expires is not None and expires <= now):
        return "EXPIRED"
    return "VALID"


def serialize_ticket(doc, now, user_cache=None):
    t = doc.to_dict() or {}
    state = ticket_state(t, now)
    expires = as_utc(t.get("expiresAt"))
    out = {
        "ticketId": t.get("ticketId") or doc.id,
        "code": t.get("code"),
        "customerId": t.get("customerId"),
        "ownerId": t.get("ownerId"),
        "serviceId": t.get("serviceId"),
        "serviceName": t.get("serviceName", "Ticket"),
        "businessName": t.get("businessName", ""),
        "quantity": int(t.get("quantity", 1)),
        "unitPrice": t.get("unitPrice", 0),
        "farePaid": t.get("farePaid", 0),
        "status": state, "state": state,
        "isValid": state == "VALID",
        "issuedAt": iso(t.get("issuedAt")),
        "expiresAt": iso(expires),
        "validatedAt": iso(t.get("validatedAt")),
        "timeRemaining": max(0, int((expires - now).total_seconds())) if (expires and state == "VALID") else 0,
    }
    cid = t.get("customerId")
    if user_cache is not None and cid:
        if cid not in user_cache:
            snap = db.collection("users").document(cid).get()
            user_cache[cid] = snap.to_dict() if snap.exists else {}
        out["customerName"] = user_cache[cid].get("fullName", "Unknown customer")
        out["customerEmail"] = user_cache[cid].get("email", "")
    return out


def sort_newest_first(items):
    return sorted(items, key=lambda x: x.get("issuedAt") or "", reverse=True)


# --------------------------------------------------------------------------- #
# Login lockout (3 failures -> 1 hour)
# --------------------------------------------------------------------------- #
def _lock_message(locked_until, now):
    remaining = max(1, int((locked_until - now).total_seconds() // 60) + 1)
    return f"Account locked due to too many failed attempts. Try again in {remaining} minute(s)."


@firestore.transactional
def _record_failed_login(transaction, ref, now):
    data = ref.get(transaction=transaction).to_dict() or {}
    locked_until = as_utc(data.get("lockoutUntil"))
    if locked_until and locked_until > now:
        return "locked", locked_until
    attempts = 0 if locked_until else int(data.get("failedLoginAttempts", 0))  # expired lock -> restart
    attempts += 1
    if attempts >= MAX_FAILED_LOGINS:
        until = now + LOCKOUT_DURATION
        transaction.update(ref, {"failedLoginAttempts": attempts, "lockoutUntil": until})
        return "locked_now", until
    transaction.update(ref, {"failedLoginAttempts": attempts, "lockoutUntil": None})
    return "fail", MAX_FAILED_LOGINS - attempts


_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _password_login(role):
    if not SECRET_KEY:
        return jsonify({"error": "Server is not configured"}), 500
    data = json_body()
    email = normalise_email(data.get("email"))
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        return jsonify({"error": "Email and password are required"}), 400

    doc = find_by_email(COLLECTION_FOR_ROLE[role], email)
    if doc is None:
        check_password_hash(_DUMMY_HASH, password)
        return jsonify({"error": "Invalid email or password."}), 401

    user = doc.to_dict()
    now = now_utc()
    locked_until = as_utc(user.get("lockoutUntil"))
    if locked_until and locked_until > now:
        return jsonify({"error": _lock_message(locked_until, now)}), 429

    password_hash = user.get("passwordHash")
    if not password_hash:
        check_password_hash(_DUMMY_HASH, password)
        return jsonify({"error": "This account signs in with Google. Use 'Continue with Google'."}), 401

    if check_password_hash(password_hash, password):
        if user.get("failedLoginAttempts") or user.get("lockoutUntil"):
            doc.reference.update({"failedLoginAttempts": 0, "lockoutUntil": None})
        return jsonify({
            "success": True, "token": issue_token(doc.id, role), "uid": doc.id,
            "role": role, "user": public_profile(user, role, doc.id),
        })

    outcome, detail = _record_failed_login(db.transaction(), doc.reference, now)
    if outcome == "locked":
        return jsonify({"error": _lock_message(detail, now)}), 429
    if outcome == "locked_now":
        return jsonify({"error": "3 failed attempts reached. Account locked for 1 hour."}), 429
    return jsonify({"error": f"Invalid email or password. {detail} attempt(s) remaining."}), 401


@app.route("/api")
def api_root():
    return jsonify({"message": "Welcome to the Tick.IT API!"})


# --------------------------------------------------------------------------- #
# Registration, login, Google
# --------------------------------------------------------------------------- #
def _validate_registration(data):
    full_name = clean_text(data.get("fullName"))
    email = normalise_email(data.get("email"))
    password = data.get("password")
    if not full_name or not email or not isinstance(password, str):
        return None, (jsonify({"error": "Missing or invalid required fields"}), 400)
    if not MIN_PASSWORD_LENGTH <= len(password) <= 128:
        return None, (jsonify({"error": f"Password must be {MIN_PASSWORD_LENGTH}-128 characters"}), 400)
    return (full_name, email, password), None


def _validate_business(data):
    name = clean_text(data.get("businessName"), 60)
    category = data.get("category")
    if len(name) < 2:
        return None, None, (jsonify({"error": "Business name must be at least 2 characters"}), 400)
    if category not in CATEGORIES:
        return None, None, (jsonify({"error": "Choose a valid business category"}), 400)
    return name, category, None


def _new_owner_record(uid, email, full_name, business_name, category):
    return {
        "uid": uid, "email": email, "fullName": full_name, "role": "owner",
        "businessName": business_name, "category": category, "bizCode": new_biz_code(),
        "totalEarnings": 0, "failedLoginAttempts": 0, "lockoutUntil": None, "createdAt": now_utc(),
    }


@app.route("/api/register/user", methods=["POST"])
@limiter.limit("10 per hour")
@require_db
def register_user():
    try:
        parsed, err = _validate_registration(json_body())
        if err:
            return err
        full_name, email, password = parsed
        if find_by_email("users", email):
            return jsonify({"error": "An account with this email already exists"}), 409
        uid = uuid.uuid4().hex
        db.collection("users").document(uid).set({
            "uid": uid, "email": email, "fullName": full_name, "role": "customer",
            "authProvider": "password", "passwordHash": generate_password_hash(password),
            "walletBalance": WELCOME_BONUS, "failedLoginAttempts": 0, "lockoutUntil": None,
            "createdAt": now_utc(),
        })
        return jsonify({"success": True, "message": "Account created", "uid": uid}), 201
    except Exception as exc:
        return server_error("register_user", exc)


@app.route("/api/register/owner", methods=["POST"])
@limiter.limit("10 per hour")
@require_db
def register_owner():
    try:
        data = json_body()
        parsed, err = _validate_registration(data)
        if err:
            return err
        full_name, email, password = parsed
        business_name, category, err = _validate_business(data)
        if err:
            return err
        if find_by_email("owners", email):
            return jsonify({"error": "An account with this email already exists"}), 409
        uid = uuid.uuid4().hex
        record = _new_owner_record(uid, email, full_name, business_name, category)
        record.update({"authProvider": "password", "passwordHash": generate_password_hash(password)})
        db.collection("owners").document(uid).set(record)
        return jsonify({"success": True, "message": "Business account created", "uid": uid}), 201
    except Exception as exc:
        return server_error("register_owner", exc)


@app.route("/api/login/user", methods=["POST"])
@limiter.limit("20 per minute")
@require_db
def login_user():
    try:
        return _password_login("customer")
    except Exception as exc:
        return server_error("login_user", exc)


@app.route("/api/login/owner", methods=["POST"])
@limiter.limit("20 per minute")
@require_db
def login_owner():
    try:
        return _password_login("owner")
    except Exception as exc:
        return server_error("login_owner", exc)


@app.route("/api/auth/google", methods=["POST"])
@limiter.limit("20 per minute")
@require_db
def auth_google():
    """Exchange a Firebase (Google) ID token for a Tick.IT session token."""
    try:
        if not SECRET_KEY:
            return jsonify({"error": "Server is not configured"}), 500
        data = json_body()
        id_token, role = data.get("idToken"), data.get("role")
        if not isinstance(id_token, str) or role not in COLLECTION_FOR_ROLE:
            return jsonify({"error": "idToken and a valid role are required"}), 400
        try:
            decoded = auth.verify_id_token(id_token)
        except Exception:
            return jsonify({"error": "Google sign-in could not be verified"}), 401

        provider = (decoded.get("firebase") or {}).get("sign_in_provider")
        email = normalise_email(decoded.get("email"))
        if provider != "google.com" or not email or not decoded.get("email_verified"):
            return jsonify({"error": "A verified Google account is required"}), 401

        collection = COLLECTION_FOR_ROLE[role]
        doc = find_by_email(collection, email)
        if doc is not None:
            doc_id, user = doc.id, doc.to_dict()   # same verified email -> existing account
        else:
            doc_id = decoded["uid"]
            name = clean_text(decoded.get("name")) or email.split("@")[0]
            if role == "customer":
                record = {"uid": doc_id, "email": email, "fullName": name, "role": "customer",
                          "walletBalance": WELCOME_BONUS, "failedLoginAttempts": 0,
                          "lockoutUntil": None, "createdAt": now_utc()}
            else:
                if not str(data.get("businessName", "")).strip():
                    return jsonify({
                        "error": "No business account for this Google account yet. Use 'Owner sign up' and enter your business details first.",
                        "code": "OWNER_SETUP_REQUIRED",
                    }), 404
                business_name, category, err = _validate_business(data)
                if err:
                    return err
                record = _new_owner_record(doc_id, email, name, business_name, category)
            record.update({"authProvider": "google", "passwordHash": None})
            db.collection(collection).document(doc_id).set(record)
            user = record

        return jsonify({
            "success": True, "token": issue_token(doc_id, role), "uid": doc_id,
            "role": role, "user": public_profile(user, role, doc_id),
        })
    except Exception as exc:
        return server_error("auth_google", exc)


# --------------------------------------------------------------------------- #
# Public (no login): what a shared booking link / scanned QR shows
# --------------------------------------------------------------------------- #
@app.route("/api/public/service/<service_id>", methods=["GET"])
@limiter.limit("60 per minute")
@require_db
def public_service(service_id):
    try:
        service_id = service_id.strip().upper()
        if not SERVICE_ID_RE.match(service_id):
            return jsonify({"error": "That doesn't look like a valid service ID"}), 400
        doc = db.collection("services").document(service_id).get()
        if not doc.exists:
            return jsonify({"error": "Service not found"}), 404
        owner = db.collection("owners").document(doc.to_dict().get("ownerId", "x")).get()
        biz = owner.to_dict().get("businessName") if owner.exists else ""
        view = service_view(doc.id, doc.to_dict(), biz)
        view["bizCode"] = owner.to_dict().get("bizCode") if owner.exists else None
        return jsonify({"success": True, **view})
    except Exception as exc:
        return server_error("public_service", exc)


@app.route("/api/public/business/<code>", methods=["GET"])
@limiter.limit("60 per minute")
@require_db
def public_business(code):
    try:
        code = code.strip().upper()
        if not BIZ_CODE_RE.match(code):
            return jsonify({"error": "Invalid business link"}), 400
        owners = list(db.collection("owners").where("bizCode", "==", code).limit(1).stream())
        if not owners:
            return jsonify({"error": "Business not found"}), 404
        owner = owners[0]
        o = owner.to_dict()
        services = [
            service_view(s.id, s.to_dict(), o.get("businessName"))
            for s in db.collection("services").where("ownerId", "==", owner.id).stream()
            if s.to_dict().get("active", True)
        ]
        services.sort(key=lambda s: s["name"] or "")
        return jsonify({"success": True, "businessName": o.get("businessName"),
                        "category": o.get("category"), "bizCode": code, "services": services})
    except Exception as exc:
        return server_error("public_business", exc)


# --------------------------------------------------------------------------- #
# Customer
# --------------------------------------------------------------------------- #
@app.route("/api/get-user-details/<user_id>", methods=["GET"])
@token_required("customer")
@require_db
def get_user_details(user_id):
    denied = require_self(user_id)
    if denied:
        return denied
    try:
        doc = db.collection("users").document(user_id).get()
        if not doc.exists:
            return jsonify({"error": "User not found"}), 404
        d = doc.to_dict()
        return jsonify({"uid": user_id, "email": d.get("email"), "fullName": d.get("fullName"),
                        "walletBalance": d.get("walletBalance", 0),
                        "authProvider": d.get("authProvider", "password")})
    except Exception as exc:
        return server_error("get_user_details", exc)


@app.route("/api/get-user-tickets/<user_id>", methods=["GET"])
@token_required("customer")
@require_db
def get_user_tickets(user_id):
    denied = require_self(user_id)
    if denied:
        return denied
    try:
        now = now_utc()
        tickets = [serialize_ticket(d, now) for d in db.collection("tickets").where("customerId", "==", user_id).stream()]
        return jsonify(sort_newest_first(tickets))
    except Exception as exc:
        return server_error("get_user_tickets", exc)


@firestore.transactional
def _pay_transaction(transaction, user_ref, service_ref, owner_ref, ticket_ref, quantity, customer_id):
    """Debit customer, credit owner and create the ticket as one atomic unit."""
    user_snap = user_ref.get(transaction=transaction)
    service_snap = service_ref.get(transaction=transaction)
    owner_snap = owner_ref.get(transaction=transaction)
    if not (user_snap.exists and service_snap.exists and owner_snap.exists):
        raise LookupError("not found")
    service = service_snap.to_dict()
    if service.get("ownerId") != owner_ref.id:
        raise LookupError("not found")
    if not service.get("active", True):
        raise ValueError("This service isn't selling tickets right now")

    unit = int(service.get("price", 0))
    total = unit * quantity
    balance = user_snap.to_dict().get("walletBalance", 0)
    if balance < total:
        raise ValueError("Insufficient funds. Please add money to your wallet.")

    issued = now_utc()
    expires = issued + timedelta(minutes=int(service.get("validityMinutes", 60)))
    ticket_id = ticket_ref.id
    owner = owner_snap.to_dict()
    transaction.update(user_ref, {"walletBalance": balance - total})
    transaction.update(owner_ref, {"totalEarnings": owner.get("totalEarnings", 0) + total})
    transaction.set(ticket_ref, {
        "ticketId": ticket_id, "code": ticket_id.replace("-", "")[:8].upper(),
        "customerId": customer_id, "ownerId": owner_ref.id,
        "serviceId": service_ref.id, "serviceName": service.get("name"),
        "businessName": owner.get("businessName"),
        "quantity": quantity, "unitPrice": unit, "farePaid": total,
        "status": "VALID", "issuedAt": issued, "expiresAt": expires, "validatedAt": None,
    })
    return {"total": total, "newBalance": balance - total, "expiresAt": expires, "service": service}


@app.route("/api/pay", methods=["POST"])
@limiter.limit("30 per minute")
@token_required("customer")
@require_db
def make_payment():
    try:
        data = json_body()
        service_id = str(data.get("serviceId", "")).strip().upper()
        try:
            quantity = int(data.get("quantity", 1))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid quantity"}), 400
        if not SERVICE_ID_RE.match(service_id):
            return jsonify({"error": "A valid serviceId is required"}), 400
        if not 1 <= quantity <= MAX_QUANTITY:
            return jsonify({"error": f"You can book 1-{MAX_QUANTITY} tickets at a time"}), 400

        service_snap = db.collection("services").document(service_id).get()
        if not service_snap.exists:
            return jsonify({"error": "Service not found"}), 404

        ticket_id = str(uuid.uuid4())
        result = _pay_transaction(
            db.transaction(),
            db.collection("users").document(g.current_user_id),
            service_snap.reference,
            db.collection("owners").document(service_snap.to_dict().get("ownerId", "x")),
            db.collection("tickets").document(ticket_id),
            quantity, g.current_user_id,
        )
        return jsonify({
            "success": True, "message": "Payment successful. Ticket generated.",
            "ticketId": ticket_id, "code": ticket_id.replace("-", "")[:8].upper(),
            "serviceName": result["service"].get("name"), "quantity": quantity,
            "farePaid": result["total"], "newBalance": result["newBalance"],
            "expiresAt": iso(result["expiresAt"]),
        }), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except LookupError:
        return jsonify({"error": "Service or account not found"}), 404
    except Exception as exc:
        return server_error("make_payment", exc)


@firestore.transactional
def _topup_transaction(transaction, ref, amount):
    snap = ref.get(transaction=transaction)
    if not snap.exists:
        raise LookupError("not found")
    new_balance = (snap.to_dict().get("walletBalance") or 0) + amount
    transaction.update(ref, {"walletBalance": new_balance})
    return new_balance


@app.route("/api/add-funds", methods=["POST"])
@limiter.limit("20 per minute")
@token_required("customer")
@require_db
def add_funds():
    # NOTE: no payment gateway sits behind this - it is a demo wallet top-up.
    try:
        try:
            amount = int(json_body().get("amount"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount"}), 400
        if amount <= 0:
            return jsonify({"error": "Amount must be greater than 0"}), 400
        if amount > MAX_TOPUP:
            return jsonify({"error": f"Maximum top-up is ₹{MAX_TOPUP} per transaction"}), 400
        new_balance = _topup_transaction(db.transaction(), db.collection("users").document(g.current_user_id), amount)
        return jsonify({"success": True, "message": f"Added ₹{amount} to wallet.", "newBalance": new_balance})
    except LookupError:
        return jsonify({"error": "User not found"}), 404
    except Exception as exc:
        return server_error("add_funds", exc)


# --------------------------------------------------------------------------- #
# Owner: profile, services, tickets
# --------------------------------------------------------------------------- #
@app.route("/api/get-owner-details/<owner_id>", methods=["GET"])
@token_required("owner")
@require_db
def get_owner_details(owner_id):
    denied = require_self(owner_id)
    if denied:
        return denied
    try:
        doc = db.collection("owners").document(owner_id).get()
        if not doc.exists:
            return jsonify({"error": "Owner not found"}), 404
        d = doc.to_dict()
        return jsonify({"uid": owner_id, "email": d.get("email"), "fullName": d.get("fullName"),
                        "businessName": d.get("businessName"), "category": d.get("category"),
                        "bizCode": d.get("bizCode"), "totalEarnings": d.get("totalEarnings", 0),
                        "authProvider": d.get("authProvider", "password")})
    except Exception as exc:
        return server_error("get_owner_details", exc)


def _parse_service_fields(data, partial=False):
    """Validate service input. Returns (updates, error_response)."""
    updates = {}
    if "name" in data or not partial:
        name = clean_text(data.get("name"), 60)
        if len(name) < 2:
            return None, (jsonify({"error": "Service name must be at least 2 characters"}), 400)
        updates["name"] = name
    if "description" in data or not partial:
        updates["description"] = clean_text(data.get("description", ""), 200)
    try:
        if "price" in data or not partial:
            price = int(data.get("price"))
            if not 0 <= price <= MAX_PRICE:
                return None, (jsonify({"error": f"Price must be between 0 and {MAX_PRICE}"}), 400)
            updates["price"] = price
        if "validityMinutes" in data or not partial:
            minutes = int(data.get("validityMinutes", 60))
            if not MIN_VALIDITY <= minutes <= MAX_VALIDITY:
                return None, (jsonify({"error": "Validity must be between 5 minutes and 30 days"}), 400)
            updates["validityMinutes"] = minutes
    except (TypeError, ValueError):
        return None, (jsonify({"error": "Price and validity must be numbers"}), 400)
    if "active" in data:
        updates["active"] = bool(data["active"])
    return updates, None


@app.route("/api/owner/services", methods=["GET"])
@token_required("owner")
@require_db
def list_services():
    try:
        owner = db.collection("owners").document(g.current_user_id).get().to_dict() or {}
        items = [service_view(d.id, d.to_dict(), owner.get("businessName"))
                 for d in db.collection("services").where("ownerId", "==", g.current_user_id).stream()]
        items.sort(key=lambda s: s.get("createdAt") or "")
        return jsonify({"success": True, "services": items, "bizCode": owner.get("bizCode")})
    except Exception as exc:
        return server_error("list_services", exc)


@app.route("/api/owner/services", methods=["POST"])
@limiter.limit("30 per hour")
@token_required("owner")
@require_db
def create_service():
    try:
        updates, err = _parse_service_fields(json_body())
        if err:
            return err
        owner = db.collection("owners").document(g.current_user_id).get().to_dict() or {}
        count = sum(1 for _ in db.collection("services").where("ownerId", "==", g.current_user_id).stream())
        if count >= 50:
            return jsonify({"error": "Service limit reached (50)"}), 400
        category = owner.get("category", "other")
        service_id = new_service_id(category)
        record = {**updates, "ownerId": g.current_user_id, "category": category,
                  "active": True, "createdAt": now_utc()}
        db.collection("services").document(service_id).set(record)
        return jsonify({"success": True, "service": service_view(service_id, record, owner.get("businessName"))}), 201
    except Exception as exc:
        return server_error("create_service", exc)


@app.route("/api/owner/services/<service_id>", methods=["PATCH"])
@token_required("owner")
@require_db
def update_service(service_id):
    try:
        ref = db.collection("services").document(service_id.strip().upper())
        snap = ref.get()
        if not snap.exists or snap.to_dict().get("ownerId") != g.current_user_id:
            return jsonify({"error": "Service not found"}), 404
        updates, err = _parse_service_fields(json_body(), partial=True)
        if err:
            return err
        if not updates:
            return jsonify({"error": "Nothing to update"}), 400
        ref.update(updates)
        owner = db.collection("owners").document(g.current_user_id).get().to_dict() or {}
        return jsonify({"success": True, "service": service_view(ref.id, {**snap.to_dict(), **updates}, owner.get("businessName"))})
    except Exception as exc:
        return server_error("update_service", exc)


@app.route("/api/get-owner-tickets/<owner_id>", methods=["GET"])
@token_required("owner")
@require_db
def get_owner_tickets(owner_id):
    denied = require_self(owner_id)
    if denied:
        return denied
    try:
        now, cache = now_utc(), {}
        tickets = [serialize_ticket(d, now, cache)
                   for d in db.collection("tickets").where("ownerId", "==", owner_id).stream()]
        return jsonify(sort_newest_first(tickets))
    except Exception as exc:
        return server_error("get_owner_tickets", exc)


@app.route("/api/sync-owner-earnings/<owner_id>", methods=["POST"])
@token_required("owner")
@require_db
def sync_owner_earnings(owner_id):
    denied = require_self(owner_id)
    if denied:
        return denied
    try:
        total = count = 0
        for doc in db.collection("tickets").where("ownerId", "==", owner_id).stream():
            total += doc.to_dict().get("farePaid", 0)
            count += 1
        db.collection("owners").document(owner_id).update({"totalEarnings": total})
        return jsonify({"success": True, "totalRevenue": total, "ticketCount": count})
    except Exception as exc:
        return server_error("sync_owner_earnings", exc)


# --------------------------------------------------------------------------- #
# Ticket validation (owner)
# --------------------------------------------------------------------------- #
@firestore.transactional
def _redeem_ticket(transaction, ticket_ref, owner_id, customer_id, now):
    """Check and consume a ticket atomically so two scans can never both succeed."""
    snap = ticket_ref.get(transaction=transaction)
    if not snap.exists:
        return 404, {"result": "INVALID", "error": "Ticket not found"}
    t = snap.to_dict()
    if t.get("ownerId") != owner_id:
        return 403, {"result": "INVALID", "error": "This ticket is not for your business"}
    if customer_id and customer_id != t.get("customerId"):
        return 400, {"result": "INVALID", "error": "Ticket does not match the customer"}

    state = ticket_state(t, now)
    base = {"ticketId": t.get("ticketId") or snap.id, "code": t.get("code"),
            "serviceName": t.get("serviceName"), "quantity": int(t.get("quantity", 1)),
            "farePaid": t.get("farePaid"), "expiresAt": iso(t.get("expiresAt")),
            "customerId": t.get("customerId")}
    if state == "USED":
        return 409, {**base, "result": "USED", "error": "ALREADY USED", "validatedAt": iso(t.get("validatedAt"))}
    if state == "EXPIRED":
        return 410, {**base, "result": "EXPIRED", "error": "Ticket has expired"}
    transaction.update(ticket_ref, {"status": "USED", "validatedAt": now, "validatedBy": owner_id})
    return 200, {**base, "result": "VALID", "validatedAt": iso(now)}


@app.route("/api/tickets/verify", methods=["POST"])
@limiter.limit("60 per minute")
@token_required("owner")
@require_db
def verify_ticket():
    """Redeem by scanned QR ({ticketId, customerId}) or by the 8-character code printed on the ticket."""
    try:
        data = json_body()
        ticket_id = str(data.get("ticketId", "")).strip()
        customer_id = str(data.get("customerId", "")).strip()
        code = str(data.get("code", "")).strip().upper()

        if ticket_id:
            if "/" in ticket_id or len(ticket_id) > 64:
                return jsonify({"result": "INVALID", "error": "Not a valid Tick.IT ticket"}), 400
            ref = db.collection("tickets").document(ticket_id)
        elif code:
            if not TICKET_CODE_RE.match(code):
                return jsonify({"result": "INVALID", "error": "Ticket codes are 8 letters/numbers"}), 400
            found = list(db.collection("tickets").where("ownerId", "==", g.current_user_id)
                         .where("code", "==", code).limit(1).stream())
            if not found:
                return jsonify({"result": "INVALID", "error": "Ticket not found"}), 404
            ref = found[0].reference
        else:
            return jsonify({"result": "INVALID", "error": "Not a valid Tick.IT ticket"}), 400

        status, body = _redeem_ticket(db.transaction(), ref, g.current_user_id, customer_id, now_utc())
        cid = body.pop("customerId", None)
        if cid and body.get("result") in ("VALID", "USED", "EXPIRED"):
            snap = db.collection("users").document(cid).get()
            if snap.exists:
                body["customerName"] = snap.to_dict().get("fullName")
        return jsonify(body), status
    except Exception as exc:
        return server_error("verify_ticket", exc)


@app.route("/api/check-ticket-validity/<ticket_id>", methods=["GET"])
@token_required("customer", "owner")
@require_db
def check_ticket_validity(ticket_id):
    """Read-only status check (does not redeem)."""
    try:
        doc = db.collection("tickets").document(ticket_id).get()
        if not doc.exists:
            return jsonify({"error": "Ticket not found"}), 404
        t = serialize_ticket(doc, now_utc())
        if g.current_user_id not in (t["customerId"], t["ownerId"]):
            return jsonify({"error": "You do not have access to this resource"}), 403
        return jsonify({k: t[k] for k in ("ticketId", "isValid", "status", "expiresAt", "farePaid", "serviceName", "quantity")})
    except Exception as exc:
        return server_error("check_ticket_validity", exc)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
