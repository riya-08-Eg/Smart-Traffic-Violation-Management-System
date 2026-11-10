"""Smart Traffic Violation & Road Safety Management System (TrafficSafe).

Flask + MySQL application with Administrator and Driver portals.
"""
import csv
import io
import os
import re
import secrets
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

import requests
import mysql.connector
from mysql.connector import Error
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-development-key")
app.config.update(
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,          # 5 MB per evidence photo
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

UPLOAD_DIR = Path(app.root_path) / "static" / "uploads" / "evidence"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
VEHICLE_TYPES = ("Car", "Motorcycle", "Bus", "Truck", "Auto-rickshaw", "Other")
STATUSES = ("Unpaid", "Paid", "Waived")
PAYMENT_METHODS = ("UPI", "Debit / Credit card", "Net banking")
PAYMENT_WINDOW_DAYS = 30          # days allowed to pay a fine before it is "Overdue"
PER_PAGE = 10

_migrated = False


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def valid_person_name(value):
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z .'’-]{1,98}", value))


def valid_license(value):
    return bool(re.fullmatch(r"[A-Za-z0-9/-]{5,40}", value))


def valid_phone(value):
    return not value or bool(re.fullmatch(r"[6-9][0-9]{9}", value))


def valid_registration(value):
    normalized = re.sub(r"[ -]", "", value.upper())
    return bool(re.fullmatch(r"[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}", normalized))


def allowed_image(filename):
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS)


def image_signature_ok(stream):
    """Check the file's magic bytes so a renamed non-image cannot be uploaded."""
    head = stream.read(12)
    stream.seek(0)
    return head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n") or (
        head[:4] == b"RIFF" and head[8:12] == b"WEBP")


# --------------------------------------------------------------------------
# Database layer
# --------------------------------------------------------------------------
def run_migrations(conn):
    """Idempotent upgrade so databases from earlier versions keep working."""
    cur = conn.cursor()
    for col, ddl in (("evidence_photo", "VARCHAR(255) NULL"),
                     ("payment_date", "DATETIME NULL"),
                     ("payment_ref", "VARCHAR(40) NULL"),
                     ("payment_method", "VARCHAR(30) NULL")):
        cur.execute("SHOW COLUMNS FROM violation_records LIKE %s", (col,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE violation_records ADD COLUMN {col} {ddl}")
    cur.execute("ALTER TABLE violation_records MODIFY status "
                "ENUM('Unpaid','Paid','Waived') NOT NULL DEFAULT 'Unpaid'")
    cur.execute("""CREATE TABLE IF NOT EXISTS appeals (
        appeal_id INT AUTO_INCREMENT PRIMARY KEY,
        record_id INT NOT NULL UNIQUE,
        driver_id INT NOT NULL,
        reason TEXT NOT NULL,
        status ENUM('Pending','Approved','Rejected') NOT NULL DEFAULT 'Pending',
        admin_remark VARCHAR(255) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at DATETIME NULL,
        CONSTRAINT fk_appeal_record FOREIGN KEY (record_id)
            REFERENCES violation_records(record_id) ON DELETE CASCADE,
        CONSTRAINT fk_appeal_driver FOREIGN KEY (driver_id)
            REFERENCES drivers(driver_id) ON DELETE CASCADE
    )""")
    conn.commit()
    cur.close()


def get_db():
    global _migrated
    conn = mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "smart_traffic_db"),
        port=int(os.environ.get("DB_PORT", "3306")),
    )
    if not _migrated:
        run_migrations(conn)
        _migrated = True
    return conn


def query_all(sql, params=()):
    conn = get_db()
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        if cur:
            cur.close()
        conn.close()


def query_one(sql, params=()):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def scalar(sql, params=()):
    row = query_one(sql, params)
    return next(iter(row.values())) if row else 0


def execute(sql, params=()):
    conn = get_db()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        if cur:
            cur.close()
        conn.close()


# --------------------------------------------------------------------------
# Security: CSRF, login throttling, authorisation
# --------------------------------------------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method == "POST":
        expected = session.get("_csrf", "")
        sent = request.form.get("_csrf", "") or request.headers.get("X-CSRFToken", "")
        if not expected or not sent or not secrets.compare_digest(expected, sent):
            flash("Your session expired or the form was invalid. Please try again.", "error")
            return redirect(url_for("home"))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


_LOGIN_FAILS = {}          # ip -> (count, first_failure_time)
MAX_FAILS, LOCK_SECONDS = 5, 300


def login_locked(ip):
    count, first = _LOGIN_FAILS.get(ip, (0, 0))
    if count >= MAX_FAILS and time.time() - first < LOCK_SECONDS:
        return int(LOCK_SECONDS - (time.time() - first)) // 60 + 1
    if count and time.time() - first >= LOCK_SECONDS:
        _LOGIN_FAILS.pop(ip, None)
    return 0


def login_failed(ip):
    count, first = _LOGIN_FAILS.get(ip, (0, time.time()))
    _LOGIN_FAILS[ip] = (count + 1, first)


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_role"):
                return redirect(url_for("login"))
            if role and session.get("user_role") != role:
                flash("You do not have permission to open that page.", "error")
                return redirect(url_for("home"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_role"):
        return redirect(url_for("home"))
    default_creds = "ADMIN_PASSWORD" not in os.environ
    selected = "admin"
    if request.method == "POST":
        ip = request.remote_addr or "?"
        selected = request.form.get("role", "admin")
        wait = login_locked(ip)
        if wait:
            flash(f"Too many failed attempts. Please try again in about {wait} minute(s).", "error")
            return render_template("login.html", selected=selected, default_creds=default_creds)
        if selected == "admin":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            ok_user = secrets.compare_digest(username.encode(), os.environ.get("ADMIN_USERNAME", "admin").encode())
            ok_pass = secrets.compare_digest(password.encode(), os.environ.get("ADMIN_PASSWORD", "admin123").encode())
            if ok_user and ok_pass:
                session.clear()
                session.permanent = True
                session.update(user_role="admin", display_name="Administrator")
                _LOGIN_FAILS.pop(ip, None)
                return redirect(url_for("admin_dashboard"))
            login_failed(ip)
            flash("Invalid administrator username or password.", "error")
        elif selected == "driver":
            license_no = request.form.get("license_no", "").strip().upper()
            phone = request.form.get("phone", "").strip()
            try:
                match = query_one("SELECT driver_id, name FROM drivers WHERE UPPER(license_no)=%s AND phone=%s",
                                  (license_no, phone))
                if match:
                    session.clear()
                    session.permanent = True
                    session.update(user_role="driver", driver_id=match["driver_id"], display_name=match["name"])
                    _LOGIN_FAILS.pop(ip, None)
                    return redirect(url_for("driver_dashboard"))
                login_failed(ip)
                flash("Details did not match our records. Check your licence and mobile number, "
                      "or ask the administrator to verify them.", "error")
            except Error as e:
                flash(f"Could not verify driver details: {e}", "error")
        else:
            selected = "admin"
            flash("Choose Administrator or Driver sign-in.", "error")
    return render_template("login.html", selected=selected, default_creds=default_creds)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/")
def home():
    if session.get("user_role") == "admin":
        return redirect(url_for("admin_dashboard"))
    if session.get("user_role") == "driver":
        return redirect(url_for("driver_dashboard"))
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Formatting helpers / template filters
# --------------------------------------------------------------------------
def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def indian_number(value, decimals=2):
    """1234567.5 -> 12,34,567.50 (Indian digit grouping)."""
    value = float(value or 0)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return sign + whole + ("." + frac if decimals else "")


@app.template_filter("inr")
def inr_filter(value, decimals=2):
    return "₹" + indian_number(value, decimals)


@app.template_filter("dfmt")
def dfmt_filter(value, fmt="%d %b %Y"):
    return as_date(value).strftime(fmt) if value else "—"


@app.template_filter("dtfmt")
def dtfmt_filter(value):
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%d %b %Y, %I:%M %p")


@app.template_filter("mask_phone")
def mask_phone(value):
    value = value or ""
    return value[:2] + "••••••" + value[-2:] if len(value) == 10 else (value or "—")


def decorate(row):
    """Add due-date / overdue helpers to a violation row."""
    d = as_date(row["violation_date"])
    row["violation_date"] = d
    row["due_date"] = d + timedelta(days=PAYMENT_WINDOW_DAYS)
    row["overdue"] = row["status"] == "Unpaid" and date.today() > row["due_date"]
    row["display_status"] = "Overdue" if row["overdue"] else row["status"]
    row["fine_amount"] = float(row["fine_amount"])
    return row


@app.context_processor
def inject_globals():
    pending = 0
    if session.get("user_role") == "admin":
        try:
            pending = scalar("SELECT COUNT(*) FROM appeals WHERE status='Pending'")
        except Error:
            pending = 0
    return {"pending_appeals": pending, "today": date.today(), "PAYMENT_WINDOW_DAYS": PAYMENT_WINDOW_DAYS}


def paginate(total, page, per_page=PER_PAGE):
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    return {"page": page, "pages": pages, "total": total, "per_page": per_page,
            "offset": (page - 1) * per_page,
            "first": 0 if not total else (page - 1) * per_page + 1,
            "last": min(page * per_page, total)}


def safe_next(target, fallback):
    """Only allow same-site relative redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return fallback


def page_arg():
    try:
        return int(request.args.get("page", 1))
    except ValueError:
        return 1


# --------------------------------------------------------------------------
# Violation queries shared by several views
# --------------------------------------------------------------------------
VIOLATION_SELECT = """SELECT vr.*, v.registration_no, v.vehicle_type, vi.violation_type,
        d.driver_id AS owner_id, d.name AS driver_name, d.license_no, d.phone,
        (SELECT a.status FROM appeals a WHERE a.record_id=vr.record_id) AS appeal_status
    FROM violation_records vr
    JOIN vehicles v ON v.vehicle_id=vr.vehicle_id
    JOIN violation_types vi ON vi.violation_type_id=vr.violation_type_id
    LEFT JOIN drivers d ON d.driver_id=v.driver_id"""


def get_violation(record_id):
    row = query_one(VIOLATION_SELECT + " WHERE vr.record_id=%s", (record_id,))
    return decorate(row) if row else None


def violation_filters(args):
    where, params = [], []
    q = args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        where.append("(v.registration_no LIKE %s OR vr.location LIKE %s OR d.name LIKE %s)")
        params += [like, like, like]
    status = args.get("status", "")
    if status in STATUSES:
        where.append("vr.status=%s")
        params.append(status)
    elif status == "Overdue":
        where.append("vr.status='Unpaid' AND vr.violation_date < %s")
        params.append(date.today() - timedelta(days=PAYMENT_WINDOW_DAYS))
    vtype = args.get("type", "")
    if vtype.isdigit():
        where.append("vr.violation_type_id=%s")
        params.append(int(vtype))
    for key, op in (("from", ">="), ("to", "<=")):
        val = args.get(key, "")
        if val:
            try:
                where.append(f"vr.violation_date {op} %s")
                params.append(date.fromisoformat(val))
            except ValueError:
                pass
    return (" WHERE " + " AND ".join(where)) if where else "", params


SORTS = {"newest": "vr.violation_date DESC, vr.record_id DESC",
         "oldest": "vr.violation_date ASC, vr.record_id ASC",
         "fine_high": "vr.fine_amount DESC, vr.record_id DESC",
         "fine_low": "vr.fine_amount ASC, vr.record_id DESC"}


def parse_violation_form(form):
    """Validate the violation form; returns cleaned values or raises ValueError."""
    vehicle_id = form.get("vehicle_id", "").strip()
    type_id = form.get("violation_type_id", "").strip()
    vdate = form.get("violation_date", "").strip()
    location = form.get("location", "").strip()
    fine = form.get("fine_amount", "").strip()
    status = form.get("status", "").strip()
    if not all([vehicle_id, type_id, vdate, location, fine, status]):
        raise ValueError("Please complete every required field. The evidence photo is optional.")
    try:
        amount = round(float(fine), 2)
    except ValueError:
        raise ValueError("Fine amount must be a number.")
    if amount <= 0 or amount > 10_000_000:
        raise ValueError("Fine amount must be greater than zero.")
    try:
        parsed = date.fromisoformat(vdate)
    except ValueError:
        raise ValueError("Enter a valid violation date.")
    if parsed > date.today():
        raise ValueError("Violation date cannot be in the future.")
    if len(location) < 3 or not re.search(r"[A-Za-z]", location):
        raise ValueError("Enter a valid location (at least 3 characters).")
    if status not in STATUSES:
        raise ValueError("Select a valid payment status.")
    if not query_one("SELECT vehicle_id FROM vehicles WHERE vehicle_id=%s", (vehicle_id,)):
        raise ValueError("Select a valid registered vehicle.")
    if not query_one("SELECT violation_type_id FROM violation_types WHERE violation_type_id=%s", (type_id,)):
        raise ValueError("Select a valid violation type.")
    return {"vehicle_id": int(vehicle_id), "type_id": int(type_id), "date": parsed,
            "location": location, "amount": amount, "status": status}


def save_photo(photo):
    """Validate and store an uploaded evidence photo; returns the stored file name."""
    if not (photo and photo.filename):
        return None
    if not allowed_image(photo.filename) or not image_signature_ok(photo.stream):
        raise ValueError("Photo must be a genuine JPG, PNG or WEBP image.")
    ext = secure_ext(photo.filename)
    name = f"evidence_{uuid.uuid4().hex}.{ext}"
    photo.save(UPLOAD_DIR / name)
    return name


def secure_ext(filename):
    return filename.rsplit(".", 1)[1].lower()


def remove_photo(name):
    if name:
        path = UPLOAD_DIR / name
        if path.is_file():
            path.unlink()


def vehicle_choices():
    return query_all("""SELECT v.vehicle_id, v.registration_no, d.name AS driver_name
                        FROM vehicles v LEFT JOIN drivers d ON d.driver_id=v.driver_id
                        ORDER BY v.registration_no""")


# --------------------------------------------------------------------------
# Administrator dashboard
# --------------------------------------------------------------------------
@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    try:
        counts = query_one("""SELECT (SELECT COUNT(*) FROM drivers) AS drivers,
                                     (SELECT COUNT(*) FROM vehicles) AS vehicles""")
        rows = [decorate(r) for r in query_all("""SELECT vr.record_id, vr.violation_date, vr.fine_amount,
                vr.status, vr.location, v.registration_no, vi.violation_type
            FROM violation_records vr JOIN vehicles v ON v.vehicle_id=vr.vehicle_id
            JOIN violation_types vi ON vi.violation_type_id=vr.violation_type_id
            ORDER BY vr.violation_date DESC, vr.record_id DESC""")]
        pending = scalar("SELECT COUNT(*) FROM appeals WHERE status='Pending'")
    except Error as e:
        return render_template("setup_error.html", error=str(e))

    total_fines = sum(r["fine_amount"] for r in rows)
    collected = sum(r["fine_amount"] for r in rows if r["status"] == "Paid")
    unpaid_amt = sum(r["fine_amount"] for r in rows if r["status"] == "Unpaid")
    this_month = date.today().replace(day=1)
    stats = {
        "drivers": counts["drivers"], "vehicles": counts["vehicles"], "violations": len(rows),
        "total_fines": total_fines, "collected": collected, "unpaid_amount": unpaid_amt,
        "overdue": sum(1 for r in rows if r["overdue"]),
        "collection_rate": round(collected / total_fines * 100) if total_fines else 0,
        "this_month": sum(1 for r in rows if r["violation_date"] >= this_month),
        "pending_appeals": pending,
    }

    # Last six calendar months, oldest first.
    months = []
    cursor = this_month
    for _ in range(6):
        months.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    monthly = [{"label": m.strftime("%b"),
                "value": sum(1 for r in rows if r["violation_date"].replace(day=1) == m)} for m in months]

    by_type = Counter(r["violation_type"] for r in rows).most_common()
    names, counts = {}, Counter()
    for r in rows:                       # group case-insensitively, show the first spelling seen
        loc = (r["location"] or "").strip()
        if loc:
            names.setdefault(loc.casefold(), loc)
            counts[loc.casefold()] += 1
    hotspots = [(names[k], n) for k, n in counts.most_common(5)]
    repeat = Counter(r["registration_no"] for r in rows).most_common(5)
    charts = {
        "monthly": monthly,
        "types": [{"label": k, "value": v} for k, v in by_type],
        "status": [{"label": "Paid", "value": sum(1 for r in rows if r["status"] == "Paid")},
                   {"label": "Unpaid", "value": sum(1 for r in rows if r["status"] == "Unpaid" and not r["overdue"])},
                   {"label": "Overdue", "value": stats["overdue"]},
                   {"label": "Waived", "value": sum(1 for r in rows if r["status"] == "Waived")}],
    }
    return render_template("dashboard.html", stats=stats, recent=rows[:6], charts=charts,
                           hotspots=hotspots, repeat=repeat)


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------
@app.route("/drivers", methods=["GET", "POST"])
@login_required("admin")
def drivers():
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "license_no", "phone")}
        if not form["name"] or not form["license_no"]:
            flash("Driver name and licence number are required.", "error")
        elif not valid_person_name(form["name"]):
            flash("Enter a valid name using letters, spaces, apostrophes, dots or hyphens (2–100 characters).", "error")
        elif not valid_license(form["license_no"]):
            flash("Licence number must be 5–40 characters: letters, numbers, / or - only.", "error")
        elif not form["phone"] or not valid_phone(form["phone"]):
            flash("A valid 10-digit Indian mobile number is required (starting with 6, 7, 8 or 9).", "error")
        else:
            try:
                execute("INSERT INTO drivers (name, license_no, phone) VALUES (%s, %s, %s)",
                        (form["name"], form["license_no"], form["phone"]))
                flash(f"Driver {form['name']} added successfully.", "success")
                return redirect(url_for("drivers"))
            except Error as e:
                flash("Could not add driver. That licence number may already be registered." if e.errno == 1062
                      else f"Could not add driver: {e}", "error")
    q = request.args.get("q", "").strip()
    where, params = "", []
    if q:
        where = " WHERE d.name LIKE %s OR d.license_no LIKE %s OR d.phone LIKE %s"
        params = [f"%{q}%"] * 3
    total = scalar(f"SELECT COUNT(*) FROM drivers d{where}", params)
    pg = paginate(total, page_arg())
    rows = query_all(f"""SELECT d.*,
            (SELECT COUNT(*) FROM vehicles v WHERE v.driver_id=d.driver_id) AS vehicle_count,
            (SELECT COUNT(*) FROM violation_records vr JOIN vehicles v ON v.vehicle_id=vr.vehicle_id
              WHERE v.driver_id=d.driver_id) AS violation_count
        FROM drivers d{where} ORDER BY d.driver_id DESC LIMIT %s OFFSET %s""",
                     params + [pg["per_page"], pg["offset"]])
    return render_template("drivers.html", drivers=rows, pg=pg, q=q, form=form)


@app.route("/drivers/<int:driver_id>")
@login_required("admin")
def driver_detail(driver_id):
    driver = query_one("SELECT * FROM drivers WHERE driver_id=%s", (driver_id,))
    if not driver:
        flash("Driver not found.", "error")
        return redirect(url_for("drivers"))
    vehicles = query_all("SELECT * FROM vehicles WHERE driver_id=%s ORDER BY registration_no", (driver_id,))
    records = [decorate(r) for r in query_all(
        VIOLATION_SELECT + " WHERE d.driver_id=%s ORDER BY vr.violation_date DESC, vr.record_id DESC", (driver_id,))]
    totals = {"violations": len(records),
              "unpaid": sum(r["fine_amount"] for r in records if r["status"] == "Unpaid"),
              "paid": sum(r["fine_amount"] for r in records if r["status"] == "Paid")}
    return render_template("driver_detail.html", driver=driver, vehicles=vehicles, records=records, totals=totals)


@app.route("/drivers/<int:driver_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def edit_driver(driver_id):
    driver = query_one("SELECT * FROM drivers WHERE driver_id=%s", (driver_id,))
    if not driver:
        flash("Driver not found.", "error")
        return redirect(url_for("drivers"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        license_no = request.form.get("license_no", "").strip()
        phone = request.form.get("phone", "").strip()
        driver.update(name=name, license_no=license_no, phone=phone)
        try:
            if not valid_person_name(name) or not valid_license(license_no) or not phone or not valid_phone(phone):
                raise ValueError("Enter a valid name, licence number and 10-digit mobile number.")
            execute("UPDATE drivers SET name=%s, license_no=%s, phone=%s WHERE driver_id=%s",
                    (name, license_no, phone, driver_id))
            flash("Driver updated successfully.", "success")
            return redirect(url_for("drivers"))
        except ValueError as e:
            flash(str(e), "error")
        except Error as e:
            flash("That licence number is already registered." if e.errno == 1062 else f"Could not update driver: {e}", "error")
    return render_template("edit_driver.html", driver=driver)


@app.route("/drivers/<int:driver_id>/delete", methods=["POST"])
@login_required("admin")
def delete_driver(driver_id):
    try:
        execute("DELETE FROM drivers WHERE driver_id=%s", (driver_id,))
        flash("Driver deleted.", "success")
    except Error:
        flash("Cannot delete this driver because a vehicle is linked to them. Reassign or delete the vehicle first.", "error")
    return redirect(url_for("drivers"))


# --------------------------------------------------------------------------
# Vehicles
# --------------------------------------------------------------------------
def driver_choices():
    return query_all("SELECT driver_id, name, license_no FROM drivers ORDER BY name")


@app.route("/vehicles", methods=["GET", "POST"])
@login_required("admin")
def vehicles():
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("registration_no", "vehicle_type", "driver_id")}
        form["registration_no"] = form["registration_no"].upper()
        if not all(form.values()):
            flash("Registration number, vehicle type and linked driver are required.", "error")
        elif not valid_registration(form["registration_no"]):
            flash("Enter a valid Indian registration number, e.g. WB01AB1234 or WB-01-AB-1234.", "error")
        elif form["vehicle_type"] not in VEHICLE_TYPES:
            flash("Select a valid vehicle type.", "error")
        elif not query_one("SELECT driver_id FROM drivers WHERE driver_id=%s", (form["driver_id"],)):
            flash("Select a valid registered driver.", "error")
        else:
            try:
                execute("INSERT INTO vehicles (registration_no, vehicle_type, driver_id) VALUES (%s, %s, %s)",
                        (form["registration_no"], form["vehicle_type"], form["driver_id"]))
                flash(f"Vehicle {form['registration_no']} registered successfully.", "success")
                return redirect(url_for("vehicles"))
            except Error as e:
                flash("Could not add vehicle. That registration number already exists." if e.errno == 1062
                      else f"Could not add vehicle: {e}", "error")
    q = request.args.get("q", "").strip()
    vtype = request.args.get("type", "")
    where, params = [], []
    if q:
        where.append("(v.registration_no LIKE %s OR d.name LIKE %s)")
        params += [f"%{q}%"] * 2
    if vtype in VEHICLE_TYPES:
        where.append("v.vehicle_type=%s")
        params.append(vtype)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    base = "FROM vehicles v LEFT JOIN drivers d ON d.driver_id=v.driver_id" + wsql
    pg = paginate(scalar("SELECT COUNT(*) " + base, params), page_arg())
    rows = query_all(f"""SELECT v.*, d.name AS driver_name,
            (SELECT COUNT(*) FROM violation_records vr WHERE vr.vehicle_id=v.vehicle_id) AS violation_count
        {base} ORDER BY v.vehicle_id DESC LIMIT %s OFFSET %s""", params + [pg["per_page"], pg["offset"]])
    return render_template("vehicles.html", vehicles=rows, drivers=driver_choices(), pg=pg, q=q,
                           vtype=vtype, vehicle_types=VEHICLE_TYPES, form=form)


@app.route("/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def edit_vehicle(vehicle_id):
    vehicle = query_one("SELECT * FROM vehicles WHERE vehicle_id=%s", (vehicle_id,))
    if not vehicle:
        flash("Vehicle not found.", "error")
        return redirect(url_for("vehicles"))
    if request.method == "POST":
        reg = request.form.get("registration_no", "").strip().upper()
        vtype = request.form.get("vehicle_type", "").strip()
        driver_id = request.form.get("driver_id", "").strip()
        vehicle.update(registration_no=reg, vehicle_type=vtype, driver_id=int(driver_id) if driver_id.isdigit() else None)
        try:
            if not valid_registration(reg) or vtype not in VEHICLE_TYPES or not driver_id:
                raise ValueError("Enter a valid registration number, vehicle type and linked driver.")
            if not query_one("SELECT driver_id FROM drivers WHERE driver_id=%s", (driver_id,)):
                raise ValueError("Select a valid registered driver.")
            execute("UPDATE vehicles SET registration_no=%s, vehicle_type=%s, driver_id=%s WHERE vehicle_id=%s",
                    (reg, vtype, driver_id, vehicle_id))
            flash("Vehicle updated successfully.", "success")
            return redirect(url_for("vehicles"))
        except ValueError as e:
            flash(str(e), "error")
        except Error as e:
            flash("That registration number already exists." if e.errno == 1062 else f"Could not update vehicle: {e}", "error")
    return render_template("edit_vehicle.html", vehicle=vehicle, drivers=driver_choices(), vehicle_types=VEHICLE_TYPES)


@app.route("/vehicles/<int:vehicle_id>/delete", methods=["POST"])
@login_required("admin")
def delete_vehicle(vehicle_id):
    try:
        execute("DELETE FROM vehicles WHERE vehicle_id=%s", (vehicle_id,))
        flash("Vehicle deleted successfully.", "success")
    except Error:
        flash("Cannot delete this vehicle because it has violation records.", "error")
    return redirect(url_for("vehicles"))


# --------------------------------------------------------------------------
# Violations
# --------------------------------------------------------------------------
@app.route("/violations", methods=["GET", "POST"])
@login_required("admin")
def violations():
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        saved = None
        try:
            data = parse_violation_form(request.form)
            saved = save_photo(request.files.get("evidence_photo"))
            paid_at = datetime.now() if data["status"] == "Paid" else None
            execute("""INSERT INTO violation_records (vehicle_id, violation_type_id, violation_date, location,
                       fine_amount, status, evidence_photo, payment_date, payment_method)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (data["vehicle_id"], data["type_id"], data["date"], data["location"], data["amount"],
                     data["status"], saved, paid_at, "Recorded by admin" if paid_at else None))
            flash("Violation record added successfully.", "success")
            return redirect(url_for("violations"))
        except (Error, ValueError) as e:
            remove_photo(saved)
            flash(f"Could not add violation: {e}", "error")

    where, params = violation_filters(request.args)
    sort = request.args.get("sort", "newest")
    order = SORTS.get(sort, SORTS["newest"])
    base = """FROM violation_records vr JOIN vehicles v ON v.vehicle_id=vr.vehicle_id
              JOIN violation_types vi ON vi.violation_type_id=vr.violation_type_id
              LEFT JOIN drivers d ON d.driver_id=v.driver_id""" + where
    pg = paginate(scalar("SELECT COUNT(*) " + base, params), page_arg())
    summary = query_one("SELECT COALESCE(SUM(vr.fine_amount),0) AS amount " + base, params)
    records = [decorate(r) for r in query_all(
        VIOLATION_SELECT + where + f" ORDER BY {order} LIMIT %s OFFSET %s", params + [pg["per_page"], pg["offset"]])]
    return render_template("violations.html", records=records, pg=pg, vehicles=vehicle_choices(),
                           types=query_all("SELECT * FROM violation_types ORDER BY violation_type"),
                           filters=request.args, sort=sort, form=form, filtered_total=float(summary["amount"]),
                           has_filters=any(request.args.get(k) for k in ("q", "status", "type", "from", "to")))


@app.route("/violations/<int:record_id>")
@login_required()
def violation_detail(record_id):
    record = get_violation(record_id)
    if not record:
        abort(404)
    if session["user_role"] == "driver" and record["owner_id"] != session["driver_id"]:
        abort(404)
    appeal = query_one("SELECT * FROM appeals WHERE record_id=%s", (record_id,))
    return render_template("violation_detail.html", r=record, appeal=appeal)


@app.route("/violations/<int:record_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def edit_violation(record_id):
    record = get_violation(record_id)
    if not record:
        flash("Violation record not found.", "error")
        return redirect(url_for("violations"))
    if request.method == "POST":
        new_photo = None
        try:
            data = parse_violation_form(request.form)
            new_photo = save_photo(request.files.get("evidence_photo"))
            photo = record["evidence_photo"]
            if new_photo or request.form.get("remove_photo"):
                remove_photo(photo)
                photo = new_photo
            paid_at, method, ref = record["payment_date"], record["payment_method"], record["payment_ref"]
            if data["status"] == "Paid" and record["status"] != "Paid":
                paid_at, method = datetime.now(), "Recorded by admin"
            elif data["status"] != "Paid":
                paid_at = method = ref = None
            execute("""UPDATE violation_records SET vehicle_id=%s, violation_type_id=%s, violation_date=%s,
                       location=%s, fine_amount=%s, status=%s, evidence_photo=%s, payment_date=%s,
                       payment_method=%s, payment_ref=%s WHERE record_id=%s""",
                    (data["vehicle_id"], data["type_id"], data["date"], data["location"], data["amount"],
                     data["status"], photo, paid_at, method, ref, record_id))
            flash("Violation record updated successfully.", "success")
            return redirect(url_for("violation_detail", record_id=record_id))
        except (Error, ValueError) as e:
            remove_photo(new_photo)
            flash(f"Could not update violation: {e}", "error")
    return render_template("edit_violation.html", record=record, vehicles=vehicle_choices(),
                           types=query_all("SELECT * FROM violation_types ORDER BY violation_type"))


@app.route("/violations/<int:record_id>/mark-paid", methods=["POST"])
@login_required("admin")
def mark_paid(record_id):
    record = get_violation(record_id)
    if not record or record["status"] != "Unpaid":
        flash("Only unpaid violations can be marked as paid.", "error")
    else:
        execute("UPDATE violation_records SET status='Paid', payment_date=%s, payment_method=%s WHERE record_id=%s",
                (datetime.now(), "Recorded by admin", record_id))
        flash(f"Record #{record_id} marked as paid.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("violations")))


@app.route("/violations/<int:record_id>/delete", methods=["POST"])
@login_required("admin")
def delete_violation(record_id):
    record = query_one("SELECT evidence_photo FROM violation_records WHERE record_id=%s", (record_id,))
    execute("DELETE FROM violation_records WHERE record_id=%s", (record_id,))
    if record:
        remove_photo(record["evidence_photo"])
    flash("Violation record deleted successfully.", "success")
    return redirect(url_for("violations"))


@app.route("/evidence/<path:filename>")
@login_required()
def evidence(filename):
    """Evidence photos are only served to the administrator or the owning driver."""
    rec = query_one("""SELECT v.driver_id FROM violation_records vr JOIN vehicles v ON v.vehicle_id=vr.vehicle_id
                       WHERE vr.evidence_photo=%s""", (filename,))
    if not rec or (session["user_role"] == "driver" and rec["driver_id"] != session["driver_id"]):
        abort(404)
    return send_from_directory(UPLOAD_DIR, filename)


# --------------------------------------------------------------------------
# Appeals
# --------------------------------------------------------------------------
@app.route("/violations/<int:record_id>/appeal", methods=["POST"])
@login_required("driver")
def submit_appeal(record_id):
    record = get_violation(record_id)
    if not record or record["owner_id"] != session["driver_id"]:
        abort(404)
    reason = request.form.get("reason", "").strip()
    if record["status"] != "Unpaid":
        flash("Only unpaid violations can be appealed.", "error")
    elif record["appeal_status"]:
        flash("An appeal has already been submitted for this violation.", "error")
    elif not 10 <= len(reason) <= 500:
        flash("Please describe your appeal in 10–500 characters.", "error")
    else:
        execute("INSERT INTO appeals (record_id, driver_id, reason, created_at) VALUES (%s,%s,%s,%s)",
                (record_id, session["driver_id"], reason, datetime.now()))
        flash("Your appeal has been submitted. The administrator will review it.", "success")
    return redirect(url_for("violation_detail", record_id=record_id))


@app.route("/appeals")
@login_required("admin")
def appeals():
    flt = request.args.get("status", "Pending")
    where, params = "", []
    if flt in ("Pending", "Approved", "Rejected"):
        where, params = " WHERE a.status=%s", [flt]
    rows = query_all(f"""SELECT a.*, vr.fine_amount, vr.violation_date, vr.location, v.registration_no,
                vi.violation_type, d.name AS driver_name, d.license_no
            FROM appeals a JOIN violation_records vr ON vr.record_id=a.record_id
            JOIN vehicles v ON v.vehicle_id=vr.vehicle_id
            JOIN violation_types vi ON vi.violation_type_id=vr.violation_type_id
            JOIN drivers d ON d.driver_id=a.driver_id{where} ORDER BY a.created_at DESC""", params)
    counts = {r["status"]: r["n"] for r in query_all("SELECT status, COUNT(*) AS n FROM appeals GROUP BY status")}
    return render_template("appeals.html", appeals=rows, flt=flt, counts=counts)


@app.route("/appeals/<int:appeal_id>/resolve", methods=["POST"])
@login_required("admin")
def resolve_appeal(appeal_id):
    decision = request.form.get("decision")
    remark = request.form.get("remark", "").strip()[:255]
    appeal = query_one("SELECT * FROM appeals WHERE appeal_id=%s", (appeal_id,))
    if not appeal or appeal["status"] != "Pending" or decision not in ("Approved", "Rejected"):
        flash("This appeal cannot be updated.", "error")
    elif decision == "Rejected" and not remark:
        flash("Please add a short remark explaining why the appeal was rejected.", "error")
    else:
        execute("UPDATE appeals SET status=%s, admin_remark=%s, resolved_at=%s WHERE appeal_id=%s",
                (decision, remark or None, datetime.now(), appeal_id))
        if decision == "Approved":
            execute("UPDATE violation_records SET status='Waived' WHERE record_id=%s AND status='Unpaid'",
                    (appeal["record_id"],))
        flash(f"Appeal {decision.lower()}." + (" The fine has been waived." if decision == "Approved" else ""), "success")
    return redirect(url_for("appeals"))


# --------------------------------------------------------------------------
# Driver portal
# --------------------------------------------------------------------------
def safety_score(records):
    """Indicative score (0-100): recent violations and overdue fines reduce it."""
    cutoff = date.today() - timedelta(days=365)
    recent = sum(1 for r in records if r["violation_date"] >= cutoff and r["status"] != "Waived")
    overdue = sum(1 for r in records if r["overdue"])
    score = max(0, 100 - recent * 8 - overdue * 10)
    label = "Excellent" if score >= 85 else "Good" if score >= 65 else "Needs attention" if score >= 40 else "At risk"
    return {"value": score, "label": label, "recent": recent, "overdue": overdue}


@app.route("/driver")
@login_required("driver")
def driver_dashboard():
    driver_id = session["driver_id"]
    driver = query_one("SELECT * FROM drivers WHERE driver_id=%s", (driver_id,))
    if not driver:
        session.clear()
        flash("Your driver profile no longer exists. Please contact the administrator.", "error")
        return redirect(url_for("login"))
    vehicles = query_all("""SELECT v.*, (SELECT COUNT(*) FROM violation_records vr WHERE vr.vehicle_id=v.vehicle_id) AS violation_count
                            FROM vehicles v WHERE v.driver_id=%s ORDER BY v.registration_no""", (driver_id,))
    records = [decorate(r) for r in query_all(
        VIOLATION_SELECT + " WHERE d.driver_id=%s ORDER BY vr.violation_date DESC, vr.record_id DESC", (driver_id,))]
    unpaid = [r for r in records if r["status"] == "Unpaid"]
    totals = {"vehicles": len(vehicles), "violations": len(records), "unpaid": len(unpaid),
              "fine_total": sum(r["fine_amount"] for r in unpaid),
              "paid_total": sum(r["fine_amount"] for r in records if r["status"] == "Paid"),
              "overdue": sum(1 for r in unpaid if r["overdue"])}
    return render_template("driver_dashboard.html", driver=driver, vehicles=vehicles, records=records,
                           unpaid=unpaid, totals=totals, score=safety_score(records))


@app.route("/driver/pay/<int:record_id>", methods=["GET", "POST"])
@login_required("driver")
def pay_fine(record_id):
    record = get_violation(record_id)
    if not record or record["owner_id"] != session["driver_id"]:
        abort(404)
    if record["status"] != "Unpaid":
        flash("This fine does not need payment.", "error")
        return redirect(url_for("violation_detail", record_id=record_id))
    if request.method == "POST":
        method = request.form.get("method", "")
        if method not in PAYMENT_METHODS:
            flash("Choose a payment method.", "error")
        else:
            ref = "TS" + datetime.now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2).upper()
            execute("""UPDATE violation_records SET status='Paid', payment_date=%s, payment_method=%s, payment_ref=%s
                       WHERE record_id=%s AND status='Unpaid'""", (datetime.now(), method, ref, record_id))
            flash("Payment successful. Your receipt is ready below.", "success")
            return redirect(url_for("receipt", record_id=record_id))
    return render_template("pay.html", r=record, methods=PAYMENT_METHODS)


@app.route("/receipt/<int:record_id>")
@login_required()
def receipt(record_id):
    record = get_violation(record_id)
    if not record or record["status"] != "Paid" or (
            session["user_role"] == "driver" and record["owner_id"] != session["driver_id"]):
        abort(404)
    return render_template("receipt.html", r=record)


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
def csv_safe(value):
    """Prevent spreadsheet formula injection."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(c) for c in row])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}-{date.today().isoformat()}.csv"})


VIOLATION_HEADER = ["Record ID", "Vehicle", "Driver", "Violation", "Date", "Location", "Fine (INR)", "Status",
                    "Due date", "Payment reference"]


def violation_csv_rows(records):
    return [[r["record_id"], r["registration_no"], r.get("driver_name") or "", r["violation_type"],
             r["violation_date"].isoformat(), r["location"], f"{r['fine_amount']:.2f}", r["display_status"],
             r["due_date"].isoformat(), r.get("payment_ref") or ""] for r in records]


@app.route("/export/<kind>.csv")
@login_required("admin")
def export_csv(kind):
    if kind == "drivers":
        rows = query_all("SELECT driver_id, name, license_no, phone FROM drivers ORDER BY driver_id")
        return csv_response("drivers", ["ID", "Name", "Licence number", "Phone"],
                            [[r["driver_id"], r["name"], r["license_no"], r["phone"]] for r in rows])
    if kind == "vehicles":
        rows = query_all("""SELECT v.vehicle_id, v.registration_no, v.vehicle_type, d.name AS driver_name
                            FROM vehicles v LEFT JOIN drivers d ON d.driver_id=v.driver_id ORDER BY v.vehicle_id""")
        return csv_response("vehicles", ["ID", "Registration", "Type", "Driver"],
                            [[r["vehicle_id"], r["registration_no"], r["vehicle_type"], r["driver_name"]] for r in rows])
    if kind == "violations":
        where, params = violation_filters(request.args)
        records = [decorate(r) for r in query_all(
            VIOLATION_SELECT + where + " ORDER BY vr.violation_date DESC, vr.record_id DESC", params)]
        return csv_response("violations", VIOLATION_HEADER, violation_csv_rows(records))
    abort(404)


@app.route("/driver/export.csv")
@login_required("driver")
def driver_export():
    records = [decorate(r) for r in query_all(
        VIOLATION_SELECT + " WHERE d.driver_id=%s ORDER BY vr.violation_date DESC", (session["driver_id"],))]
    return csv_response("my-violations", VIOLATION_HEADER, violation_csv_rows(records))


# --------------------------------------------------------------------------
# Live traffic
# --------------------------------------------------------------------------
@app.route("/api/traffic-incidents")
@login_required()
def traffic_incidents():
    """Live incidents for the Kolkata area, supplied by the TomTom Traffic API."""
    api_key = os.environ.get("TOMTOM_API_KEY", "").strip()
    if not api_key:
        return jsonify({"configured": False, "incidents": [],
                        "message": "Live traffic is not configured yet. Add your TomTom API key as TOMTOM_API_KEY."})
    params = {
        "key": api_key,
        "bbox": os.environ.get("TRAFFIC_BBOX", "88.20,22.35,88.55,22.70"),
        "language": "en-GB",
        "timeValidityFilter": "present",
        "categoryFilter": "1,3,6,7,8,9,11,14",
        "fields": "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,events{description},from,to,delay,length,roadNumbers,lastReportTime}}}",
    }
    names = {1: "Accident", 3: "Dangerous conditions", 6: "Heavy traffic", 7: "Lane closed", 8: "Road closed",
             9: "Road works", 11: "Flooding", 14: "Broken-down vehicle"}
    try:
        response = requests.get("https://api.tomtom.com/traffic/services/5/incidentDetails", params=params, timeout=15)
        response.raise_for_status()
        incidents = []
        for item in response.json().get("incidents", []):
            props = item.get("properties", {}) or {}
            geom = item.get("geometry", {}) or {}
            coords = geom.get("coordinates", [])
            if geom.get("type") == "Point" and len(coords) >= 2:
                lon, lat = coords[0], coords[1]
            elif geom.get("type") == "LineString" and coords:
                mid = coords[len(coords) // 2]
                lon, lat = mid[0], mid[1]
            else:
                continue
            events = props.get("events", []) or []
            incidents.append({
                "id": props.get("id"), "category": names.get(props.get("iconCategory", 0), "Traffic incident"),
                "description": events[0].get("description") if events else "Traffic incident reported",
                "from": props.get("from") or "Nearby road", "to": props.get("to") or "",
                "delay": props.get("delay", 0), "magnitude": props.get("magnitudeOfDelay", 0),
                "lat": lat, "lon": lon, "lastReportTime": props.get("lastReportTime")})
        return jsonify({"configured": True, "updated": True, "incidents": incidents})
    except requests.RequestException as exc:
        return jsonify({"configured": True, "updated": False, "incidents": [],
                        "message": f"Traffic provider could not be reached: {exc}"}), 502
    except (ValueError, KeyError):
        return jsonify({"configured": True, "updated": False, "incidents": [],
                        "message": "Unexpected response from traffic provider."}), 502


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, title="Page not found",
                           message="The page or record you are looking for does not exist, or you do not have access to it."), 404


@app.errorhandler(413)
def too_large(_):
    flash("That file is too large. Evidence photos must be 5 MB or smaller.", "error")
    return redirect(request.referrer or url_for("home"))


@app.errorhandler(Error)
def database_error(err):
    return render_template("setup_error.html", error=str(err)), 500


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1")
