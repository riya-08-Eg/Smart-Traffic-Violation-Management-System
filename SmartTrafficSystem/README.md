# TrafficSafe — Smart Traffic Violation & Road Safety Management System

A DBMS PBL web application built with **Flask (Python)**, **MySQL** and plain HTML/CSS/JavaScript.
Two role-based portals: **Administrator** and **Driver**.

## What's included

### Administrator portal
- **Dashboard** – KPI cards, violations-per-month chart, fine-collection progress, status and violation-type charts, hotspot locations, repeat offenders, live traffic map and recent records.
- **Drivers / Vehicles / Violations** – full create, read, update, delete with **search, filters, sorting and pagination**; data is kept in the form if validation fails.
- **Driver profile page** – vehicles, violations and balances for one driver.
- **Violation detail ("challan") page** – timeline, evidence photo, printable notice.
- **Mark as paid** in one click, **overdue** detection (fines unpaid 30 days after the offence).
- **Appeals** – review driver appeals; approving one waives the fine, rejecting needs a remark.
- **CSV export** of drivers, vehicles and (filtered) violations.

### Driver portal
- Personal dashboard with unpaid fines, totals, profile and a road-safety score.
- **Pay fines online** (simulated payment: UPI / card / net banking) and view or print a **receipt**.
- **Appeal a fine** with a written reason and track the decision.
- Searchable, filterable violation history; download own records as CSV.
- Live traffic map with severity filters and new-alert notifications.

### Platform improvements
- Redesigned UI: consistent design system, SVG icons, responsive layout with mobile menu, **dark mode**, toast messages, confirmation dialogs, empty states, print styles, keyboard/screen-reader friendly.
- **CSRF protection** on every form, login throttling (5 failures → 5-minute lock), secure session cookies.
- Evidence photos are verified by file signature and are served **only to the admin or the owning driver**.
- Old photos are deleted when a record is replaced or removed.
- Indian number formatting (₹12,34,567.00) throughout.
- Automatic, safe database migration for older databases.

## Setup

1. **Create the database** – run `database/schema.sql` in MySQL Workbench / phpMyAdmin / terminal.
   It also inserts demo drivers, vehicles and violations (only if the tables are empty).
2. **Install dependencies**
   ```bash
   python -m venv venv
   venv\Scripts\activate          # Windows
   source venv/bin/activate       # macOS / Linux
   python -m pip install -r requirements.txt
   ```
3. **Configure (optional)** – defaults are `localhost`, port `3306`, user `root`, empty password, database `smart_traffic_db`.

   PowerShell:
   ```powershell
   $env:DB_PASSWORD="your_mysql_password"
   $env:ADMIN_USERNAME="admin"
   $env:ADMIN_PASSWORD="Choose-A-Strong-Password"
   $env:FLASK_SECRET_KEY="a-long-random-string"
   $env:TOMTOM_API_KEY="your_tomtom_key"     # optional, enables live traffic
   ```
4. **Run** – `python app.py` and open http://127.0.0.1:5000

### Demo logins
| Role | Credentials |
|---|---|
| Administrator | `admin` / `admin123` (change with `ADMIN_USERNAME` / `ADMIN_PASSWORD`; the hint disappears once you set a password) |
| Driver | licence `WB-TEST-1001`, mobile `9000000001` (or `WB-TEST-1002` / `9000000002`) |

## Suggested demo flow
1. Sign in as admin → show the dashboard charts.
2. Add a driver, register a vehicle, record a violation with a photo.
3. Use the Violations filters (status, type, date) and export a CSV.
4. Sign out, sign in as the driver → pay a fine, open the receipt.
5. Submit an appeal on another fine, then sign in as admin → **Appeals** → approve it and see the fine become *Waived*.

## Configuration reference
| Variable | Purpose | Default |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MySQL connection | localhost / 3306 / root / empty / smart_traffic_db |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Admin login | admin / admin123 |
| `FLASK_SECRET_KEY` | Session signing key | development key (change it!) |
| `FLASK_DEBUG` | `0` to disable debug mode | `1` |
| `TOMTOM_API_KEY` | Live traffic incidents | not set |
| `TRAFFIC_BBOX` | Map area `minLon,minLat,maxLon,maxLat` | Kolkata |

## Notes
- Academic demonstration only. Payments are simulated; no real money moves.
- Before any real deployment: set a strong secret key and admin password, use HTTPS and a production WSGI server (e.g. gunicorn/waitress), hash driver credentials properly, and use a dedicated MySQL user instead of `root`.
- The map uses Leaflet + OpenStreetMap, and the font loads from Google Fonts, so an internet connection is needed for those.
