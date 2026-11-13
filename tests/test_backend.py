"""Backend tests using an in-memory Firestore + stubbed firebase_admin / flask_limiter.
Run:  python tests/test_backend.py
"""
import os, sys, types, uuid
from datetime import datetime, timedelta, timezone

# ---------- in-memory Firestore ----------
class Snap:
    def __init__(self, id, data, ref): self.id, self._d, self.reference = id, data, ref
    @property
    def exists(self): return self._d is not None
    def to_dict(self): return dict(self._d) if self._d is not None else None
class Ref:
    def __init__(self, store, col, id): self.s, self.col, self.id = store, col, id
    def get(self, transaction=None): return Snap(self.id, self.s.data.get(self.col, {}).get(self.id), self)
    def set(self, d): self.s.data.setdefault(self.col, {})[self.id] = dict(d)
    def update(self, d): self.s.data[self.col][self.id].update(d)
class Query:
    def __init__(self, store, col, filters=(), lim=None): self.s, self.col, self.f, self.lim = store, col, filters, lim
    def where(self, f, op, v): return Query(self.s, self.col, self.f + ((f, v),), self.lim)
    def limit(self, n): return Query(self.s, self.col, self.f, n)
    def stream(self):
        out = [Snap(i, d, Ref(self.s, self.col, i)) for i, d in self.s.data.get(self.col, {}).items()
               if all(d.get(f) == v for f, v in self.f)]
        return iter(out[: self.lim] if self.lim else out)
class Col(Query):
    def __init__(self, store, col): super().__init__(store, col)
    def document(self, id=None): return Ref(self.s, self.col, id or uuid.uuid4().hex)
class Txn:
    def __init__(self): self.ops = []
    def update(self, ref, d): self.ops.append(("u", ref, d))
    def set(self, ref, d): self.ops.append(("s", ref, d))
class DB:
    def __init__(self): self.data = {}
    def collection(self, n): return Col(self, n)
    def transaction(self): return Txn()
def transactional(fn):
    def run(txn, *a, **k):
        res = fn(txn, *a, **k)
        for op, ref, d in txn.ops:
            ref.update(d) if op == "u" else ref.set(d)
        return res
    return run

fake_db = DB()
fa = types.ModuleType("firebase_admin"); fa._apps = [1]
cr = types.ModuleType("firebase_admin.credentials"); cr.Certificate = lambda x: x
fs = types.ModuleType("firebase_admin.firestore"); fs.client = lambda: fake_db; fs.transactional = transactional
au = types.ModuleType("firebase_admin.auth")
GOOGLE = {}
def verify(tok):
    if tok not in GOOGLE: raise ValueError("bad")
    return GOOGLE[tok]
au.verify_id_token = verify
fa.credentials, fa.firestore, fa.auth = cr, fs, au
sys.modules.update({"firebase_admin": fa, "firebase_admin.credentials": cr, "firebase_admin.firestore": fs, "firebase_admin.auth": au})
fl = types.ModuleType("flask_limiter")
class Limiter:
    def __init__(self, *a, **k): pass
    def limit(self, *a, **k): return lambda f: f
fl.Limiter = Limiter; sys.modules["flask_limiter"] = fl
if "flask_cors" not in sys.modules:
    try: import flask_cors
    except ImportError:
        m = types.ModuleType("flask_cors"); m.CORS = lambda *a, **k: None; sys.modules["flask_cors"] = m

os.environ["JWT_SECRET"] = "test-secret"
os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"] = "{}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import main, jwt
c = main.app.test_client()

fails = 0
def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if not cond else ""))
    fails += 0 if cond else 1
def req(method, url, body=None, tok=None):
    return getattr(c, method)(url, json=body, headers={"Authorization": f"Bearer {tok}"} if tok else {})
post = lambda u, b=None, t=None: req("post", u, b, t)
get = lambda u, t=None: req("get", u, None, t)
patch = lambda u, b=None, t=None: req("patch", u, b, t)

# ---------- registration ----------
r = post("/api/register/user", {"fullName": "Ann <b>Lee</b>", "email": "Ann@x.com", "password": "password123"})
check("customer registers", r.status_code == 201); uid = r.get_json()["uid"]
check("name sanitised + hashed pw", fake_db.data["users"][uid]["fullName"] == "Ann Lee" and fake_db.data["users"][uid]["passwordHash"].startswith(("scrypt", "pbkdf2")))
check("short password rejected", post("/api/register/user", {"fullName": "A", "email": "a@x.com", "password": "short"}).status_code == 400)
check("duplicate email 409", post("/api/register/user", {"fullName": "A", "email": "ann@x.com", "password": "password123"}).status_code == 409)
check("owner needs valid category", post("/api/register/owner", {"fullName": "Bob", "email": "bob@x.com", "password": "password123", "businessName": "Eco Park", "category": "nope"}).status_code == 400)
check("owner needs business name", post("/api/register/owner", {"fullName": "Bob", "email": "bob@x.com", "password": "password123", "businessName": "", "category": "park"}).status_code == 400)
r = post("/api/register/owner", {"fullName": "Bob", "email": "bob@x.com", "password": "password123", "businessName": "Eco <i>Park</i>", "category": "park"})
check("owner registers", r.status_code == 201); oid = r.get_json()["uid"]
check("owner gets bizCode + clean name", len(fake_db.data["owners"][oid]["bizCode"]) == 6 and fake_db.data["owners"][oid]["businessName"] == "Eco Park")

# ---------- lockout ----------
for left in (2, 1):
    r = post("/api/login/user", {"email": "ann@x.com", "password": "wrong"})
    check(f"wrong pw -> 401, {left} left", r.status_code == 401 and f"{left} attempt" in r.get_json()["error"], r.get_json())
r = post("/api/login/user", {"email": "ann@x.com", "password": "wrong"})
check("3rd failure -> 429 locked 1 hour", r.status_code == 429 and "1 hour" in r.get_json()["error"])
check("correct pw during lock still 429", post("/api/login/user", {"email": "ann@x.com", "password": "password123"}).status_code == 429)
fake_db.data["users"][uid]["lockoutUntil"] = main.now_utc() - timedelta(seconds=1)
r = post("/api/login/user", {"email": "ann@x.com", "password": "wrong"})
check("after expiry counter restarts", r.status_code == 401 and "2 attempt" in r.get_json()["error"])
utok = post("/api/login/user", {"email": "ann@x.com", "password": "password123"}).get_json()["token"]
otok = post("/api/login/owner", {"email": "bob@x.com", "password": "password123"}).get_json()["token"]
check("customer creds fail on owner login", post("/api/login/owner", {"email": "ann@x.com", "password": "password123"}).status_code == 401)

# ---------- auth enforcement ----------
check("pay w/o token 401", post("/api/pay", {"serviceId": "PRK-AAAAAA"}).status_code == 401)
exp = jwt.encode({"userId": uid, "role": "customer", "exp": main.now_utc() - timedelta(hours=1)}, "test-secret", algorithm="HS256")
check("expired token 401", post("/api/pay", {"serviceId": "PRK-AAAAAA"}, exp).status_code == 401)
check("customer can't create services 403", post("/api/owner/services", {"name": "x1", "price": 1}, utok).status_code == 403)
check("owner can't pay 403", post("/api/pay", {"serviceId": "PRK-AAAAAA"}, otok).status_code == 403)
check("IDOR blocked", get(f"/api/get-user-details/{oid}", utok).status_code == 403)

# ---------- services ----------
check("bad price rejected", post("/api/owner/services", {"name": "Entry", "price": -5, "validityMinutes": 60}, otok).status_code == 400)
check("bad validity rejected", post("/api/owner/services", {"name": "Entry", "price": 50, "validityMinutes": 1}, otok).status_code == 400)
r = post("/api/owner/services", {"name": "Adult Entry", "description": "<script>x</script>Day pass", "price": 50, "validityMinutes": 1440}, otok)
check("service created", r.status_code == 201); sid = r.get_json()["service"]["serviceId"]
check("ID format PRK-XXXXXX", main.SERVICE_ID_RE.match(sid) is not None, sid)
check("description sanitised", "<" not in fake_db.data["services"][sid]["description"])
sid2 = post("/api/owner/services", {"name": "Free Child Entry", "price": 0, "validityMinutes": 60}, otok).get_json()["service"]["serviceId"]
check("list services", len(get("/api/owner/services", otok).get_json()["services"]) == 2)

# ---------- public pages ----------
r = c.get(f"/api/public/service/{sid}")
check("public service needs no login", r.status_code == 200 and r.get_json()["price"] == 50 and r.get_json()["businessName"] == "Eco Park")
check("public service hides owner details", "ownerId" not in r.get_json() and "email" not in str(r.get_json()))
check("public service lowercase id ok", c.get(f"/api/public/service/{sid.lower()}").status_code == 200)
check("public bad id 400", c.get("/api/public/service/hello").status_code == 400)
check("public unknown 404", c.get("/api/public/service/PRK-ZZZZZZ").status_code == 404)
biz = c.get(f"/api/public/business/{fake_db.data['owners'][oid]['bizCode']}").get_json()
check("business link lists services", biz["businessName"] == "Eco Park" and len(biz["services"]) == 2)
patch(f"/api/owner/services/{sid2}", {"active": False}, otok)
check("paused service hidden from business page", len(c.get(f"/api/public/business/{fake_db.data['owners'][oid]['bizCode']}").get_json()["services"]) == 1)

# ---------- pay ----------
check("paused service can't be bought", post("/api/pay", {"serviceId": sid2}, utok).status_code == 400)
patch(f"/api/owner/services/{sid2}", {"active": True}, otok)
r2 = post("/api/register/owner", {"fullName": "Eve", "email": "eve@x.com", "password": "password123", "businessName": "Parking Co", "category": "parking"})
etok = post("/api/login/owner", {"email": "eve@x.com", "password": "password123"}).get_json()["token"]
check("other owner can't edit my service (404)", patch(f"/api/owner/services/{sid}", {"price": 1}, etok).status_code == 404)
check("quantity 0 rejected", post("/api/pay", {"serviceId": sid, "quantity": 0}, utok).status_code == 400)
check("quantity 11 rejected", post("/api/pay", {"serviceId": sid, "quantity": 11}, utok).status_code == 400)
check("unknown service 404", post("/api/pay", {"serviceId": "PRK-ZZZZZZ"}, utok).status_code == 404)
r = post("/api/pay", {"serviceId": sid, "quantity": 3, "price": 1}, utok)
check("pay 3 x 50 = 150 (client price ignored)", r.status_code == 201 and r.get_json()["farePaid"] == 150 and r.get_json()["newBalance"] == 350, r.get_json())
tid, code = r.get_json()["ticketId"], r.get_json()["code"]
check("owner credited 150", fake_db.data["owners"][oid]["totalEarnings"] == 150)
t = fake_db.data["tickets"][tid]
check("ticket snapshot", t["serviceName"] == "Adult Entry" and t["quantity"] == 3 and t["businessName"] == "Eco Park" and t["expiresAt"].tzinfo is not None)
check("validity = 1 day", abs((t["expiresAt"] - t["issuedAt"]).total_seconds() - 86400) < 2)
fake_db.data["users"][uid]["walletBalance"] = 10
check("insufficient funds 400", post("/api/pay", {"serviceId": sid, "quantity": 1}, utok).status_code == 400)
check("free ticket works with 0 price", post("/api/pay", {"serviceId": sid2}, utok).status_code == 201)
check("wallet unchanged by free ticket", fake_db.data["users"][uid]["walletBalance"] == 10)
fake_db.data["users"][uid]["walletBalance"] = 500
check("add-funds cap", post("/api/add-funds", {"amount": 10**6}, utok).status_code == 400)
tk = get(f"/api/get-user-tickets/{uid}", utok).get_json()
check("customer sees tickets with Z times", len(tk) == 2 and tk[0]["expiresAt"].endswith("Z"))
ot = get(f"/api/get-owner-tickets/{oid}", otok).get_json()
check("owner sees customer name", ot[0]["customerName"] == "Ann Lee")

# ---------- verify ----------
check("customer can't verify 403", post("/api/tickets/verify", {"ticketId": tid, "customerId": uid}, utok).status_code == 403)
check("other business can't redeem 403", post("/api/tickets/verify", {"ticketId": tid, "customerId": uid}, etok).status_code == 403)
check("customer mismatch 400", post("/api/tickets/verify", {"ticketId": tid, "customerId": "zzz"}, otok).status_code == 400)
check("other business can't redeem by code", post("/api/tickets/verify", {"code": code}, etok).status_code == 404)
r = post("/api/tickets/verify", {"ticketId": tid, "customerId": uid}, otok)
b = r.get_json()
check("QR scan VALID + admit 3", r.status_code == 200 and b["result"] == "VALID" and b["quantity"] == 3 and b["customerName"] == "Ann Lee" and b["serviceName"] == "Adult Entry", b)
check("status USED", fake_db.data["tickets"][tid]["status"] == "USED")
r = post("/api/tickets/verify", {"ticketId": tid, "customerId": uid}, otok)
check("2nd scan ALREADY USED 409", r.status_code == 409 and r.get_json()["result"] == "USED")
r = post("/api/tickets/verify", {"code": code.lower()}, otok)
check("manual code also ALREADY USED", r.status_code == 409)
tid3 = post("/api/pay", {"serviceId": sid}, utok).get_json()
r = post("/api/tickets/verify", {"code": tid3["code"]}, otok)
check("manual code redeems", r.status_code == 200 and r.get_json()["result"] == "VALID")
check("bad code format 400", post("/api/tickets/verify", {"code": "abc"}, otok).status_code == 400)
tid4 = post("/api/pay", {"serviceId": sid}, utok).get_json()["ticketId"]
fake_db.data["tickets"][tid4]["expiresAt"] = main.now_utc() - timedelta(minutes=1)
r = post("/api/tickets/verify", {"ticketId": tid4, "customerId": uid}, otok)
check("expired 410, not consumed", r.status_code == 410 and fake_db.data["tickets"][tid4]["status"] == "VALID")
check("unknown ticket 404", post("/api/tickets/verify", {"ticketId": "nope", "customerId": uid}, otok).status_code == 404)

# ---------- Google ----------
check("bad Google token 401", post("/api/auth/google", {"idToken": "bad", "role": "customer"}).status_code == 401)
GOOGLE["g1"] = {"uid": "guid1", "email": "gina@gmail.com", "email_verified": True, "name": "Gina G", "firebase": {"sign_in_provider": "google.com"}}
r = post("/api/auth/google", {"idToken": "g1", "role": "customer"})
check("Google creates customer", r.status_code == 200 and fake_db.data["users"]["guid1"]["authProvider"] == "google")
check("2nd Google login reuses account", post("/api/auth/google", {"idToken": "g1", "role": "customer"}).get_json()["uid"] == "guid1")
check("Google acct has no password login", post("/api/login/user", {"email": "gina@gmail.com", "password": "whatever1"}).status_code == 401)
GOOGLE["g2"] = {"uid": "guid2", "email": "new@gmail.com", "email_verified": True, "name": "New", "firebase": {"sign_in_provider": "google.com"}}
r = post("/api/auth/google", {"idToken": "g2", "role": "owner"})
check("Google owner w/o business -> OWNER_SETUP_REQUIRED", r.status_code == 404 and r.get_json()["code"] == "OWNER_SETUP_REQUIRED")
r = post("/api/auth/google", {"idToken": "g2", "role": "owner", "businessName": "Shiv Mandir", "category": "temple"})
check("Google creates owner", r.status_code == 200 and fake_db.data["owners"]["guid2"]["bizCode"])
GOOGLE["g3"] = {"uid": "x", "email": "pw@x.com", "email_verified": True, "firebase": {"sign_in_provider": "password"}}
check("non-Google token rejected", post("/api/auth/google", {"idToken": "g3", "role": "customer"}).status_code == 401)
GOOGLE["g4"] = {"uid": "y", "email": "unv@gmail.com", "email_verified": False, "firebase": {"sign_in_provider": "google.com"}}
check("unverified email rejected", post("/api/auth/google", {"idToken": "g4", "role": "customer"}).status_code == 401)

h = c.get("/api").headers
check("security headers", h.get("X-Content-Type-Options") == "nosniff")
print("\n%s" % ("ALL PASSED" if not fails else f"{fails} FAILED"))
sys.exit(1 if fails else 0)
