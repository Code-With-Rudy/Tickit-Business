# Tick.IT - QR tickets for any local business

## How it works
* **Business owner** signs up with a business name + category (Park, Temple, Parking, Ride, Event, Other), then adds one or more **services** (e.g. "Adult Entry ₹50, valid 1 day", "2-hour parking ₹30", "Auto ride ₹25, valid 30 min"). Each service gets an ID like `PRK-7K3QM2`, a QR code, and a booking link. Owners can also share one link listing all their services.
* **Customer** scans the QR with any phone camera or the in-app scanner, opens the shared link (WhatsApp, Instagram...), or types the service ID. They log in, pick how many tickets (1-10), pay from their wallet, and get a ticket QR plus an 8-character backup code.
* **Owner validates** at the gate by scanning the ticket QR (or typing the backup code). The screen says VALID - Redeemed with "ADMIT N", or ALREADY USED / EXPIRED. A ticket can be redeemed only once.
* Prices and totals are always calculated on the server. Paused services can't be bought. Services are never deleted so ticket history stays intact.

## Pages
`index.html` (login/sign up) · `book.html?s=SERVICEID` or `?b=BUSINESSCODE` (public booking page) · `dashboard.html` (customer) · `owner_dashboard.html` (business)

# Security & setup

## 0. Fresh start checklist (new repo + new Vercel + new Firebase; old ones untouched)
1. **New Firebase project** (console.firebase.google.com -> Add project, name it e.g. `tickit-v2`). Do not reuse the old one.
2. **Firestore**: Build -> Firestore Database -> Create database -> *Production mode*, location `asia-south1` (Mumbai). Then Rules tab -> paste `firestore.rules` -> Publish.
3. **Service account**: Project settings -> Service accounts -> Generate new private key. Keep the JSON private.

4. **Web app + Google sign-in**: Project settings -> Your apps -> add Web app, copy the config into `public/js/firebase-config.js`. Authentication -> Get started -> Sign-in method -> enable Google.

5. **New GitHub repo**: unzip this project, `git init`, commit, push to a new empty repo (`.gitignore` already blocks secrets).

6. **New Vercel project**: Import the new repo. Before the first deploy add the environment variables in section 1.

7. **Authorized domain**: after the first deploy, add the `*.vercel.app` domain (and any custom one) in Firebase -> Authentication -> Settings -> Authorized domains, then redeploy if you changed the config.

8. **Smoke test** (section 5 of the Testing list below): register an owner, open the vehicle QR, register a rider on a phone, scan, pay, show ticket QR, scan it as the owner, scan again (must say ALREADY USED).


## 1. Environment variables (Vercel -> Project Settings -> Environment Variables)
| Name | Required | Notes |
|---|---|---|
| `JWT_SECRET` | **yes** | Long random string: `python -c "import secrets;print(secrets.token_hex(32))"`. Without it the API refuses to issue/verify tokens in production. |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | yes | Firebase service-account JSON (already used before). |
| `RATELIMIT_STORAGE_URI` | recommended | e.g. a Redis URL. Vercel runs many short-lived instances, so the default in-memory limiter is per-instance only. |
| `ALLOWED_ORIGINS` | no | Comma-separated. Leave empty: the site and API share one origin, so CORS is off. |

## 2. Google Sign-In
1. Firebase console -> Authentication -> Sign-in method -> enable **Google**.
2. Authentication -> Settings -> Authorized domains -> add your Vercel domain.
3. Project settings -> Your apps -> Web app -> copy the config into `public/js/firebase-config.js`.

## 3. What changed
* Login now checks passwords (it previously accepted any email). 3 wrong passwords -> HTTP 429, locked 1 hour. Google login is not subject to the password lockout.
* Passwords are hashed with werkzeug in Firestore (`passwordHash`). New password accounts no longer create Firebase Auth users.
* Every endpoint except register/login/Google requires `Authorization: Bearer <token>`; the acting user comes from the token, so users can't read or spend another user's data by changing an ID.
* `/api/pay` is one Firestore transaction (debit rider, credit owner, create ticket).
* Ticket status is now `VALID | USED | EXPIRED`; all times are UTC (`...Z`).
* Endpoints: `POST /api/auth/google`, `GET /api/public/service/<id>`, `GET /api/public/business/<code>` (no login, read-only), `GET|POST /api/owner/services`, `PATCH /api/owner/services/<id>`, `POST /api/pay`, `POST /api/tickets/verify`.
* Frontend: scripts moved to `public/js/` so the CSP in `vercel.json` can forbid inline scripts; tokens live in `sessionStorage`; user-supplied text is escaped before rendering.

## 4. Things to know
* **Collections**: `users` (customers), `owners` (businesses), `services`, `tickets`. This version is for a fresh database; old vehicle-based data is not read.
* **Service QR codes contain the booking URL** (`https://your-site/book.html?s=ID`), so a normal phone camera opens the page. They never contain a price: change a price any time without reprinting the poster.
* **Add funds** is still a demo top-up with no payment gateway (now capped at ₹10,000 per request).
* Camera scanning needs HTTPS (Vercel is fine; `localhost` also works).

## 5. Tests
`python tests/test_backend.py` runs the backend against an in-memory Firestore (no Firebase needed).
