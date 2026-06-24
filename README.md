# 🔥 Shawaya POS

A dual-mode Flask POS system:
- **Server / Admin** — manage products, categories, serial keys, and view synced orders  
- **Desktop Kiosk** — customer-facing ordering screen, works **offline**, syncs when internet returns

---

## Architecture

```
[OWNER phone/laptop]
       │  HTTPS (ngrok)
       ▼
[ngrok tunnel] ◄──── [Admin Server :5000] ◄──── sync ──── [Kiosk :5001]
                                                              │
                                              works offline, queues orders
                                              syncs automatically when net back
```

---

## Requirements

- Python **3.10 or newer**
- pip (comes with Python)
- A modern browser (Chrome, Edge, Firefox)

---

## Quick Setup (New Device)

### 1. Copy the project folder and open a terminal inside it

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file
```bash
copy .env.example .env        # Windows
cp   .env.example .env        # Mac / Linux
```

Open `.env` and fill in the two required secrets — generate them with:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Run that twice and paste the output as `SECRET_KEY` and `SYNC_API_KEY`.

---

## Running Locally (same machine, no internet needed)

Open **two terminals**:

```bash
# Terminal 1 — Admin server
python run_server.py          # → http://localhost:5000/admin/login

# Terminal 2 — Kiosk
python run_desktop.py         # → http://localhost:5001
```

---

## Making the Admin Dashboard Public (ngrok)

So the **owner can access the dashboard from anywhere** using mobile data.

### Step 1 — Create a free ngrok account

1. Go to **https://ngrok.com** and sign up (free)
2. From **https://dashboard.ngrok.com/authtokens** — copy your auth token
3. From **https://dashboard.ngrok.com/domains** — click **"New Domain"** to get your **free static domain**  
   It looks like: `your-name.ngrok-free.app`

### Step 2 — Add to `.env` on the server machine

```env
NGROK_AUTH_TOKEN=your-token-from-dashboard
NGROK_DOMAIN=your-name.ngrok-free.app
```

### Step 3 — Start the public server

```bash
python run_public.py
```

You will see output like:
```
======================================================
  🔥  SHAWAYA POS — ONLINE
======================================================

  🌐 Public URL (owner dashboard):
     https://your-name.ngrok-free.app/admin/login

  🖥️  Local URL (same machine):
     http://localhost:5000/admin/login

  📋 Kiosk .env — set these on every kiosk machine:
     SERVER_URL=https://your-name.ngrok-free.app
     SYNC_API_KEY=<your-sync-key>

  Press Ctrl+C to stop.
======================================================
```

The owner can now open `https://your-name.ngrok-free.app/admin/login` on their phone or laptop from **anywhere in the world**.

### Step 4 — Update the kiosk `.env`

On the kiosk machine, set:
```env
APP_MODE=desktop
SERVER_URL=https://your-name.ngrok-free.app
SYNC_API_KEY=same-key-as-server
```

Then start the kiosk normally:
```bash
python run_desktop.py
```

---

## Offline Mode (Kiosk)

The kiosk is built to handle internet outages automatically:

| Situation | What happens |
|---|---|
| **Internet works** | Orders sync to server every 30 seconds (configurable) |
| **Internet drops** | Kiosk keeps working — orders saved locally as `pending` |
| **Internet returns** | Background sync thread automatically pushes all pending orders |
| **Long outage (>7 days offline)** | Serial key re-validation required |

The owner will see all orders in the dashboard as soon as the kiosk reconnects — nothing is lost.

---

## LAN Setup (kiosk on one PC, server on another, no ngrok)

On the **server machine**:
```bash
python run_server.py
# Find your local IP: run `ipconfig` (Windows) or `ip addr` (Linux/Mac)
```

On the **kiosk machine** `.env`:
```env
APP_MODE=desktop
SERVER_URL=http://192.168.1.X:5000    # ← server machine's local IP
SYNC_API_KEY=same-key-as-server
```

---

## First Login

1. Open the admin URL → login with `admin` / `changeme123`
2. You are forced to **change the password** on first login
3. Go to **Serial Keys → Generate** to create a kiosk activation key

---

## Environment Variables

| Variable | Default | Used by | Description |
|---|---|---|---|
| `SECRET_KEY` | *(required)* | both | Flask session secret |
| `SYNC_API_KEY` | *(required)* | both | Shared kiosk↔server key |
| `APP_MODE` | `server` | both | `server` or `desktop` |
| `SERVER_URL` | `http://localhost:5000` | kiosk | Where to sync to |
| `SYNC_INTERVAL` | `30` | kiosk | Seconds between syncs |
| `NGROK_AUTH_TOKEN` | *(optional)* | server | For `run_public.py` |
| `NGROK_DOMAIN` | *(optional)* | server | Your free static ngrok domain |
| `ADMIN_DEFAULT_USERNAME` | `admin` | server | First-run admin username |
| `ADMIN_DEFAULT_PASSWORD` | `changeme123` | server | First-run admin password |
| `FLASK_DEBUG` | `0` | both | `1` = debug mode (dev only) |

---

## Running Tests

```bash
python -m pytest tests/ -v
# Expected: 16 passed
```

---

## Project Structure

```
shawaya/
├── app.py              # App factory (mode-aware)
├── config.py           # Dev / Prod configuration
├── models.py           # SQLAlchemy models
├── sync.py             # Background sync thread
├── routes/
│   ├── admin.py        # Admin dashboard
│   ├── api.py          # Sync API (server only)
│   └── desktop.py      # Kiosk UI + local API
├── static/             # CSS + JS
├── templates/          # HTML templates
├── tests/              # 16-test pytest suite
├── run_server.py       # Start admin server (local)
├── run_desktop.py      # Start kiosk
├── run_public.py       # Start server + ngrok tunnel
├── requirements.txt
└── .env.example
```
