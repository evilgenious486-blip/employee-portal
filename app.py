from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from openpyxl import load_workbook
import io
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "employee_portal.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "doc", "docx", "xlsx"}
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-key"
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
UPLOAD_FOLDER.mkdir(exist_ok=True)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def calculate_hours(check_in: str | None, check_out: str | None) -> float:
    if not check_in or not check_out:
        return 0.0
    try:
        start = datetime.strptime(check_in, "%H:%M")
        end = datetime.strptime(check_out, "%H:%M")
        diff = (end - start).total_seconds() / 3600
        return round(max(diff, 0), 2)
    except Exception:
        return 0.0



def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS leave_approval_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        leave_application_id INTEGER NOT NULL,
        step_no INTEGER NOT NULL,
        approver_user_id INTEGER NOT NULL,
        approver_title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Waiting',
        remarks TEXT,
        action_at TEXT,
        FOREIGN KEY (leave_application_id) REFERENCES leave_applications (id),
        FOREIGN KEY (approver_user_id) REFERENCES users (id)
    )""")
    default_departments = ["Electrical", "Civil", "Mechanical", "QA/QC", "HSE", "Planning", "Procurement", "Testing & Commissioning", "Protection / SCADA", "Project Management", "HR", "Administration"]
    default_designations = ["Worker", "Technician", "Helper", "Supervisor", "Foreman", "Department Engineer", "Safety Officer", "Safety Engineer", "HSE Engineer", "Site Manager", "Project Engineer", "Project Manager", "HR Officer"]
    for dept in default_departments:
        db.execute("INSERT OR IGNORE INTO departments(name) VALUES (?)", (dept,))
    for desig in default_designations:
        db.execute("INSERT OR IGNORE INTO designations(name) VALUES (?)", (desig,))
    user_cols = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "monthly_basic" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN monthly_basic REAL NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN default_allowances REAL NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN deduction_per_absent REAL NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN deduction_per_late REAL NOT NULL DEFAULT 0")
        user_cols = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_filename" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN avatar_filename TEXT")
    attendance_cols = {row[1] for row in db.execute("PRAGMA table_info(attendance)").fetchall()}
    if "ot_hours" not in attendance_cols:
        db.execute("ALTER TABLE attendance ADD COLUMN ot_hours REAL NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS holiday_calendar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holiday_date TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        holiday_type TEXT NOT NULL DEFAULT 'Holiday',
        created_at TEXT NOT NULL
    )""")
    db.commit()


def get_role_options() -> list[str]:
    return ["employee", "manager", "hr", "admin"]


def normalize_name(value: str | None) -> str:
    return (value or "").strip().lower()


def designation_name_by_id(db: sqlite3.Connection, designation_id: int | None) -> str:
    if not designation_id:
        return ""
    row = db.execute("SELECT name FROM designations WHERE id=?", (designation_id,)).fetchone()
    return row["name"] if row else ""


def find_approver_by_designation(db: sqlite3.Connection, designation_names: list[str], department_id: int | None = None, exclude_user_id: int | None = None) -> sqlite3.Row | None:
    for designation_name in designation_names:
        params: list[Any] = [designation_name]
        query = """
            SELECT u.id, u.full_name, ds.name AS designation_name, d.name AS department_name
            FROM users u
            JOIN designations ds ON u.designation_id = ds.id
            LEFT JOIN departments d ON u.department_id = d.id
            WHERE lower(ds.name)=lower(?) AND u.is_active=1
        """
        if department_id is not None:
            query += " AND u.department_id=?"
            params.append(department_id)
        if exclude_user_id is not None:
            query += " AND u.id<>?"
            params.append(exclude_user_id)
        query += " ORDER BY u.id LIMIT 1"
        row = db.execute(query, tuple(params)).fetchone()
        if row:
            return row
    return None


def find_hr_approver(db: sqlite3.Connection, exclude_user_id: int | None = None) -> sqlite3.Row | None:
    query = """
        SELECT u.id, u.full_name, COALESCE(ds.name, 'HR') AS designation_name, d.name AS department_name
        FROM users u
        LEFT JOIN designations ds ON u.designation_id = ds.id
        LEFT JOIN departments d ON u.department_id = d.id
        WHERE u.role='hr' AND u.is_active=1
    """
    params: list[Any] = []
    if exclude_user_id is not None:
        query += " AND u.id<>?"
        params.append(exclude_user_id)
    query += " ORDER BY u.id LIMIT 1"
    return db.execute(query, tuple(params)).fetchone()


def build_leave_route(applicant: sqlite3.Row) -> list[dict[str, Any]]:
    db = get_db()
    designation = normalize_name(applicant["designation_name"] if "designation_name" in applicant.keys() else designation_name_by_id(db, applicant["designation_id"]))
    department_id = applicant["department_id"]
    route: list[dict[str, Any]] = []

    def add_step(approver: sqlite3.Row | None, fallback_title: str) -> None:
        if not approver:
            return
        if approver["id"] == applicant["id"]:
            return
        if any(step["approver_user_id"] == approver["id"] for step in route):
            return
        route.append({
            "approver_user_id": approver["id"],
            "approver_title": approver["designation_name"] or fallback_title,
        })

    supervisor = find_approver_by_designation(db, ["Supervisor", "Foreman"], department_id, applicant["id"])
    dept_engineer = find_approver_by_designation(db, ["Department Engineer"], department_id, applicant["id"])
    site_manager = find_approver_by_designation(db, ["Site Manager"], None, applicant["id"])
    project_engineer = find_approver_by_designation(db, ["Project Engineer"], None, applicant["id"])
    project_manager = find_approver_by_designation(db, ["Project Manager"], None, applicant["id"])
    hr_user = find_hr_approver(db, applicant["id"])

    if any(term in designation for term in ["worker", "technician", "helper"]):
        add_step(supervisor, "Supervisor")
        add_step(dept_engineer, "Department Engineer")
        add_step(site_manager, "Site Manager")
        add_step(project_engineer, "Project Engineer")
        add_step(project_manager, "Project Manager")
        add_step(hr_user, "HR")
    elif any(term in designation for term in ["supervisor", "foreman"]):
        add_step(dept_engineer, "Department Engineer")
        add_step(site_manager, "Site Manager")
        add_step(project_engineer, "Project Engineer")
        add_step(project_manager, "Project Manager")
        add_step(hr_user, "HR")
    elif designation == "department engineer" or ("engineer" in designation and not any(term in designation for term in ["project engineer", "safety engineer", "hse engineer"])):
        add_step(site_manager, "Site Manager")
        add_step(project_engineer, "Project Engineer")
        add_step(project_manager, "Project Manager")
        add_step(hr_user, "HR")
    elif any(term in designation for term in ["safety officer", "safety engineer", "hse engineer"]):
        add_step(site_manager, "Site Manager")
        add_step(project_engineer, "Project Engineer")
        add_step(project_manager, "Project Manager")
        add_step(hr_user, "HR")
    elif designation == "site manager":
        add_step(project_engineer, "Project Engineer")
        add_step(project_manager, "Project Manager")
        add_step(hr_user, "HR")
    elif designation == "project engineer":
        add_step(project_manager, "Project Manager")
        add_step(hr_user, "HR")
    elif designation == "project manager":
        add_step(hr_user, "HR")
    elif applicant["role"] == "hr":
        add_step(hr_user, "HR")
    else:
        if applicant["manager_id"]:
            mgr = db.execute("""
                SELECT u.id, u.full_name, COALESCE(ds.name, 'Manager') AS designation_name, d.name AS department_name
                FROM users u
                LEFT JOIN designations ds ON u.designation_id=ds.id
                LEFT JOIN departments d ON u.department_id=d.id
                WHERE u.id=? AND u.is_active=1
            """, (applicant["manager_id"],)).fetchone()
            add_step(mgr, "Manager")
        add_step(project_manager or site_manager or project_engineer, "Manager")
        add_step(hr_user, "HR")
    return route


def create_leave_approval_route(leave_id: int, applicant: sqlite3.Row) -> list[sqlite3.Row]:
    db = get_db()
    route = build_leave_route(applicant)
    if not route:
        return []
    for idx, step in enumerate(route, start=1):
        db.execute(
            "INSERT INTO leave_approval_steps(leave_application_id, step_no, approver_user_id, approver_title, status) VALUES (?, ?, ?, ?, ?)",
            (leave_id, idx, step["approver_user_id"], step["approver_title"], "Pending" if idx == 1 else "Waiting"),
        )
    return db.execute("SELECT * FROM leave_approval_steps WHERE leave_application_id=? ORDER BY step_no", (leave_id,)).fetchall()


def get_pending_leave_step(leave_id: int):
    return get_db().execute("SELECT * FROM leave_approval_steps WHERE leave_application_id=? AND status='Pending' ORDER BY step_no LIMIT 1", (leave_id,)).fetchone()


def refresh_leave_status(leave_id: int) -> None:
    db = get_db()
    leave = db.execute("SELECT * FROM leave_applications WHERE id=?", (leave_id,)).fetchone()
    if not leave:
        return
    pending = get_pending_leave_step(leave_id)
    if pending:
        approver = db.execute("SELECT full_name FROM users WHERE id=?", (pending["approver_user_id"],)).fetchone()
        stage_text = approver["full_name"] if approver else pending["approver_title"]
        db.execute("UPDATE leave_applications SET status=?, current_stage=? WHERE id=?", (f"Pending {pending['approver_title']} Approval", f"pending_step_{pending['step_no']}", leave_id))
        return
    approved_count = db.execute("SELECT COUNT(*) AS c FROM leave_approval_steps WHERE leave_application_id=? AND status='Approved'", (leave_id,)).fetchone()["c"]
    total_count = db.execute("SELECT COUNT(*) AS c FROM leave_approval_steps WHERE leave_application_id=?", (leave_id,)).fetchone()["c"]
    if total_count and approved_count == total_count:
        db.execute("UPDATE leave_applications SET status='Final Approved', manager_status='Approved', hr_status='Approved', current_stage='closed' WHERE id=?", (leave_id,))


def get_holiday_row(db: sqlite3.Connection, attendance_date: str | None):
    if not attendance_date:
        return None
    return db.execute("SELECT * FROM holiday_calendar WHERE holiday_date=?", (attendance_date,)).fetchone()


def compute_ot_hours(db: sqlite3.Connection, attendance_date: str | None, status: str, hours_worked: float, manual_ot: float | None = None) -> float:
    if manual_ot is not None:
        return round(max(manual_ot, 0), 2)
    holiday = get_holiday_row(db, attendance_date)
    if holiday and hours_worked > 0 and status in {"Present", "Late", "Half Day", "Holiday", "Vacation"}:
        return round(hours_worked, 2)
    return 0.0


def upsert_payroll_from_attendance(user_id: int, month_value: str) -> None:
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return
    month_prefix = datetime.strptime(month_value, "%Y-%m").strftime("%Y-%m")
    rows = db.execute("SELECT * FROM attendance WHERE user_id=? AND substr(attendance_date,1,7)=?", (user_id, month_prefix)).fetchall()
    late_days = sum(1 for r in rows if r["status"] == "Late")
    absent_days = sum(1 for r in rows if r["status"] == "Absent")
    half_days = sum(1 for r in rows if r["status"] == "Half Day")
    day_rate = (user["monthly_basic"] or 0) / 30 if (user["monthly_basic"] or 0) else 0
    absent_rate = user["deduction_per_absent"] or day_rate
    deductions = round(absent_days * absent_rate + half_days * 0.5 * absent_rate + late_days * (user["deduction_per_late"] or 0), 2)
    net_salary = round((user["monthly_basic"] or 0) + (user["default_allowances"] or 0) - deductions, 2)
    month_label = datetime.strptime(month_value, "%Y-%m").strftime("%b %Y")
    existing = db.execute("SELECT id FROM payroll_slips WHERE user_id=? AND month_label=?", (user_id, month_label)).fetchone()
    if existing:
        db.execute("UPDATE payroll_slips SET basic_salary=?, allowances=?, deductions=?, net_salary=?, generated_at=? WHERE id=?", ((user["monthly_basic"] or 0), (user["default_allowances"] or 0), deductions, net_salary, now_str(), existing["id"]))
    else:
        db.execute("INSERT INTO payroll_slips(user_id, month_label, basic_salary, allowances, deductions, net_salary, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, month_label, (user["monthly_basic"] or 0), (user["default_allowances"] or 0), deductions, net_salary, now_str()))


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        ensure_schema(g.db)
    return g.db


@app.teardown_appcontext
def close_db(exception: Exception | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    schema = BASE_DIR / "schema.sql"
    with closing(sqlite3.connect(DATABASE)) as db:
        with open(schema, "r", encoding="utf-8") as f:
            db.executescript(f.read())
        db.commit()
    seed_data()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_image_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute(
        """
        SELECT u.*, d.name AS department_name, ds.name AS designation_name, m.full_name AS manager_name
        FROM users u
        LEFT JOIN departments d ON u.department_id = d.id
        LEFT JOIN designations ds ON u.designation_id = ds.id
        LEFT JOIN users m ON u.manager_id = m.id
        WHERE u.id = ?
        """,
        (user_id,),
    ).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if current_user() is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def role_required(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("You do not have access to this page.", "danger")
                return redirect(url_for("dashboard"))
            return view(**kwargs)

        return wrapped_view

    return decorator


def log_audit(module_name: str, action_name: str, detail: str, target_user_id: int | None = None) -> None:
    get_db().execute(
        "INSERT INTO audit_logs(actor_user_id, target_user_id, module_name, action_name, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session.get("user_id"), target_user_id, module_name, action_name, detail, now_str()),
    )


def create_notification(user_id: int, title: str, message: str, link: str | None = None) -> None:
    get_db().execute(
        "INSERT INTO notifications(user_id, title, message, link, is_read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
        (user_id, title, message, link, now_str()),
    )


def queue_email(to_user_id: int, subject: str, body: str) -> None:
    db = get_db()
    recipient = db.execute("SELECT email FROM users WHERE id = ?", (to_user_id,)).fetchone()
    if recipient:
        db.execute(
            "INSERT INTO email_queue(to_user_id, to_email, subject, body, status, created_at) VALUES (?, ?, ?, ?, 'Queued', ?)",
            (to_user_id, recipient["email"], subject, body, now_str()),
        )


def notify_user(user_id: int, title: str, message: str, link: str | None = None, email_subject: str | None = None, email_body: str | None = None) -> None:
    create_notification(user_id, title, message, link)
    queue_email(user_id, email_subject or title, email_body or message)


def app_counts(user: sqlite3.Row) -> dict[str, Any]:
    db = get_db()
    counts: dict[str, Any] = {}
    counts["my_leave_count"] = db.execute("SELECT COUNT(*) AS c FROM leave_applications WHERE user_id = ?", (user["id"],)).fetchone()["c"]
    counts["my_pending_count"] = db.execute("SELECT COUNT(*) AS c FROM leave_applications WHERE user_id = ? AND status LIKE 'Pending%'", (user["id"],)).fetchone()["c"]
    counts["my_balance"] = db.execute("SELECT COALESCE(SUM(remaining_days),0) AS c FROM leave_balances WHERE user_id = ?", (user["id"],)).fetchone()["c"]
    counts["attendance_days"] = db.execute("SELECT COUNT(*) AS c FROM attendance WHERE user_id = ? AND status='Present'", (user["id"],)).fetchone()["c"]
    counts["payslip_count"] = db.execute("SELECT COUNT(*) AS c FROM payroll_slips WHERE user_id = ?", (user["id"],)).fetchone()["c"]
    counts["unread_notifications"] = db.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND is_read = 0", (user["id"],)).fetchone()["c"]
    if user["role"] == "manager":
        counts["team_members"] = db.execute("SELECT COUNT(*) AS c FROM users WHERE manager_id = ? AND is_active = 1", (user["id"],)).fetchone()["c"]
    pending_for_me = db.execute("SELECT COUNT(*) AS c FROM leave_approval_steps WHERE approver_user_id=? AND status='Pending'", (user["id"],)).fetchone()["c"]
    counts["pending_for_me"] = pending_for_me
    if user["role"] in {"hr", "admin"}:
        counts["total_employees"] = db.execute("SELECT COUNT(*) AS c FROM users WHERE is_active = 1").fetchone()["c"]
        counts["documents_total"] = db.execute("SELECT COUNT(*) AS c FROM employee_documents").fetchone()["c"]
    if user["role"] == "admin":
        counts["queued_emails"] = db.execute("SELECT COUNT(*) AS c FROM email_queue WHERE status = 'Queued'").fetchone()["c"]
    return counts


@app.context_processor
def inject_globals():
    user = current_user()
    settings = None
    unread_count = 0
    if user:
        db = get_db()
        try:
            settings = db.execute("SELECT * FROM company_settings ORDER BY id DESC LIMIT 1").fetchone()
            unread_count = db.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND is_read=0", (user["id"],)).fetchone()["c"]
        except sqlite3.OperationalError:
            settings = None
    return {"current_user": user, "year": date.today().year, "settings": settings, "unread_notification_count": unread_count}


def seed_data() -> None:
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    if db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] > 0:
        db.close()
        return

    departments = [
        "Electrical", "Mechanical", "Civil", "Safety", "Operations", "Project Management",
        "HR", "Administration", "Logistics", "Support"
    ]
    designations = [
        "Project Manager", "Operation Manager", "Site Manager", "Site Engineer", "Civil Engineer",
        "Mechanical Engineer", "Safety Engineer", "HR Officer", "System Admin",
        "Civil QC Engineer", "Electrical QC Engineer", "Site Civil Engineer", "Assistant Surveyor",
        "Electrical Technician", "Electrician", "Technician", "Safety Officer", "Driver",
        "Lineman", "Lineman & Store Assistant", "Day Security", "Night Watchman", "Labour", "Tea Boy"
    ]
    for dept in departments:
        db.execute("INSERT INTO departments(name) VALUES (?)", (dept,))
    for desig in designations:
        db.execute("INSERT INTO designations(name) VALUES (?)", (desig,))

    dept_ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM departments").fetchall()}
    desig_ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM designations").fetchall()}

    leave_types = [
        ("Annual Leave", 21, 1),
        ("Sick Leave", 14, 1),
        ("Emergency Leave", 7, 1),
        ("Unpaid Leave", 0, 0),
        ("Casual Leave", 7, 1),
    ]
    db.executemany("INSERT INTO leave_types(name, annual_quota, is_paid) VALUES (?, ?, ?)", leave_types)

    users = [
        {
            "full_name": "Project Manager",
            "email": "projectmanager@example.com",
            "employee_code": "PAC-PM-001",
            "password": "Project@123",
            "role": "manager",
            "department": "Project Management",
            "designation": "Project Manager",
            "manager_email": None,
            "phone": "+966500000001",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001111",
            "join_date": "2024-01-01",
        },
        {
            "full_name": "Yureed Taseer",
            "email": "opmanager@example.com",
            "employee_code": "PAC-OPS-001",
            "password": "Operation@123",
            "role": "manager",
            "department": "Operations",
            "designation": "Operation Manager",
            "manager_email": "projectmanager@example.com",
            "phone": "+966500000002",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001112",
            "join_date": "2024-02-01",
        },
        {
            "full_name": "Muhammad Waseem",
            "email": "manager@example.com",
            "employee_code": "PAC-249",
            "password": "Muhammad@123",
            "role": "manager",
            "department": "Electrical",
            "designation": "Site Manager",
            "manager_email": "opmanager@example.com",
            "phone": "+966500000003",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001113",
            "join_date": "2024-03-15",
        },
        {
            "full_name": "Ali Site Engineer",
            "email": "employee@example.com",
            "employee_code": "PAC-SE-001",
            "password": "Employee@123",
            "role": "employee",
            "department": "Electrical",
            "designation": "Site Engineer",
            "manager_email": "manager@example.com",
            "phone": "+966500000004",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001114",
            "join_date": "2025-01-10",
        },
        {
            "full_name": "Faisal Malik",
            "email": "faisal.malik@example.com",
            "employee_code": "PAC-127",
            "password": "Faisal@123",
            "role": "employee",
            "department": "Civil",
            "designation": "Civil QC Engineer",
            "manager_email": "manager@example.com",
            "phone": "+966500000005",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001115",
            "join_date": "2025-02-01",
        },
        {
            "full_name": "Mobeen Ahmad",
            "email": "mobeen.ahmad@example.com",
            "employee_code": "PAC-424",
            "password": "Mobeen@123",
            "role": "employee",
            "department": "Safety",
            "designation": "Safety Officer",
            "manager_email": "manager@example.com",
            "phone": "+966500000006",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001116",
            "join_date": "2025-02-15",
        },
        {
            "full_name": "Qazi Ehsan ul Haq Budder",
            "email": "ehsan.budder@example.com",
            "employee_code": "PAC-450",
            "password": "Qazi@123",
            "role": "employee",
            "department": "Civil",
            "designation": "Site Civil Engineer",
            "manager_email": "manager@example.com",
            "phone": "+966500000010",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001120",
            "join_date": "2025-03-01",
        },
        {
            "full_name": "Usama Jaleel",
            "email": "usama.jaleel@example.com",
            "employee_code": "PAC-452",
            "password": "Usama@123",
            "role": "employee",
            "department": "Civil",
            "designation": "Assistant Surveyor",
            "manager_email": "manager@example.com",
            "phone": "+966500000011",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001121",
            "join_date": "2025-03-05",
        },
        {
            "full_name": "Abid Ali",
            "email": "abid.ali@example.com",
            "employee_code": "PAC-455",
            "password": "Abid@123",
            "role": "employee",
            "department": "Electrical",
            "designation": "Electrical Technician",
            "manager_email": "manager@example.com",
            "phone": "+966500000012",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001122",
            "join_date": "2025-03-10",
        },
        {
            "full_name": "Shabir Khan",
            "email": "shabir.khan@example.com",
            "employee_code": "PAC-421",
            "password": "Shabir@123",
            "role": "employee",
            "department": "Administration",
            "designation": "Night Watchman",
            "manager_email": "manager@example.com",
            "phone": "+966500000013",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001123",
            "join_date": "2025-03-10",
        },
        {
            "full_name": "Sagir Mridha",
            "email": "sagir.mridha@example.com",
            "employee_code": "PAC-457",
            "password": "Sagir@123",
            "role": "employee",
            "department": "Logistics",
            "designation": "Driver",
            "manager_email": "manager@example.com",
            "phone": "+966500000014",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001124",
            "join_date": "2025-03-12",
        },
        {
            "full_name": "Ashfaq Ahmad",
            "email": "ashfaq.ahmad@example.com",
            "employee_code": "PAC-295",
            "password": "Ashfaq@123",
            "role": "employee",
            "department": "Electrical",
            "designation": "Electrical QC Engineer",
            "manager_email": "manager@example.com",
            "phone": "+966500000015",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001125",
            "join_date": "2025-03-15",
        },
        {
            "full_name": "Vijendra Singh",
            "email": "vijendra.singh@example.com",
            "employee_code": "PAC-462",
            "password": "Vijendra@123",
            "role": "employee",
            "department": "Electrical",
            "designation": "Electrician",
            "manager_email": "manager@example.com",
            "phone": "+966500000016",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001126",
            "join_date": "2025-03-18",
        },
        {
            "full_name": "Salahudddin",
            "email": "salahudddin@example.com",
            "employee_code": "PAC-313",
            "password": "Salahuddin@123",
            "role": "employee",
            "department": "Safety",
            "designation": "Safety Engineer",
            "manager_email": "manager@example.com",
            "phone": "+966500000017",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001127",
            "join_date": "2025-03-18",
        },
        {
            "full_name": "Mirza Samiulla Baig",
            "email": "mirza.samiulla@example.com",
            "employee_code": "PAC-224",
            "password": "Mirza@123",
            "role": "employee",
            "department": "Mechanical",
            "designation": "Mechanical Engineer",
            "manager_email": "manager@example.com",
            "phone": "+966500000018",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001128",
            "join_date": "2025-03-20",
        },
        {
            "full_name": "Muhammad Ashfaq",
            "email": "muhammad.ashfaq@example.com",
            "employee_code": "PAC-324",
            "password": "MuhammadAshfaq@123",
            "role": "employee",
            "department": "Mechanical",
            "designation": "Technician",
            "manager_email": "manager@example.com",
            "phone": "+966500000019",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001129",
            "join_date": "2025-03-22",
        },
        {
            "full_name": "Sayem",
            "email": "sayem@example.com",
            "employee_code": "PAC-476",
            "password": "Sayem@123",
            "role": "employee",
            "department": "Support",
            "designation": "Tea Boy",
            "manager_email": "manager@example.com",
            "phone": "+966500000020",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001130",
            "join_date": "2025-03-22",
        },
        {
            "full_name": "Suhel Prdhan",
            "email": "suhel.prdhan@example.com",
            "employee_code": "PAC-190",
            "password": "Suhel@123",
            "role": "employee",
            "department": "Support",
            "designation": "Labour",
            "manager_email": "manager@example.com",
            "phone": "+966500000021",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001131",
            "join_date": "2025-03-22",
        },
        {
            "full_name": "Adnan Yaqob",
            "email": "adnan.yaqob@example.com",
            "employee_code": "PAC-376",
            "password": "Adnan@123",
            "role": "employee",
            "department": "Electrical",
            "designation": "Lineman",
            "manager_email": "manager@example.com",
            "phone": "+966500000022",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001132",
            "join_date": "2025-03-22",
        },
        {
            "full_name": "Umair Ali",
            "email": "umair.ali@example.com",
            "employee_code": "PAC-378",
            "password": "Umair@123",
            "role": "employee",
            "department": "Electrical",
            "designation": "Lineman & Store Assistant",
            "manager_email": "manager@example.com",
            "phone": "+966500000023",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001133",
            "join_date": "2025-03-22",
        },
        {
            "full_name": "Ghanem Alsqour",
            "email": "ghanem.alsqour@example.com",
            "employee_code": "PAC-464",
            "password": "Ghanem@123",
            "role": "employee",
            "department": "Administration",
            "designation": "Day Security",
            "manager_email": "manager@example.com",
            "phone": "+966500000024",
            "address": "Dammam, Saudi Arabia",
            "emergency_contact": "+966500001134",
            "join_date": "2025-03-22",
        },
        {
            "full_name": "Hina HR",
            "email": "hr@example.com",
            "employee_code": "EMP1008",
            "password": "HR@12345",
            "role": "hr",
            "department": "HR",
            "designation": "HR Officer",
            "manager_email": None,
            "phone": "+966500000008",
            "address": "Khobar, Saudi Arabia",
            "emergency_contact": "+966500001118",
            "join_date": "2023-08-10",
        },
        {
            "full_name": "Super Admin",
            "email": "admin@example.com",
            "employee_code": "EMP1009",
            "password": "Admin@123",
            "role": "admin",
            "department": "Administration",
            "designation": "System Admin",
            "manager_email": None,
            "phone": "+966500000009",
            "address": "Riyadh, Saudi Arabia",
            "emergency_contact": "+966500001119",
            "join_date": "2023-01-01",
        },
    ]

    user_id_by_email = {}
    for row in users:
        manager_id = user_id_by_email.get(row["manager_email"])
        db.execute(
            """
            INSERT INTO users(full_name, email, employee_code, password_hash, role, department_id, designation_id, manager_id, phone, address, emergency_contact, join_date, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                row["full_name"],
                row["email"],
                row["employee_code"],
                generate_password_hash(row["password"]),
                row["role"],
                dept_ids[row["department"]],
                desig_ids[row["designation"]],
                manager_id,
                row["phone"],
                row["address"],
                row["emergency_contact"],
                row["join_date"],
            ),
        )
        user_id_by_email[row["email"]] = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    for user_id in user_id_by_email.values():
        for leave_type_id, leave_def in enumerate(leave_types, start=1):
            default_total = leave_def[1]
            db.execute(
                "INSERT INTO leave_balances(user_id, leave_type_id, total_days, used_days, remaining_days) VALUES (?, ?, ?, 0, ?)",
                (user_id, leave_type_id, default_total, default_total),
            )

    db.execute(
        "INSERT INTO company_settings(company_name, leave_workflow, default_working_hours, allow_document_upload) VALUES (?, ?, ?, ?)",
        ("Pacost International", "Site Engineer / Site Staff → Site Manager → HR Final Review", 8.0, 1),
    )

    created_at = now_str()
    site_engineer_id = user_id_by_email["employee@example.com"]
    site_manager_id = user_id_by_email["manager@example.com"]
    hr_id = user_id_by_email["hr@example.com"]
    admin_id = user_id_by_email["admin@example.com"]

    db.execute(
        """
        INSERT INTO leave_applications(application_no, user_id, leave_type_id, from_date, to_date, total_days, reason, attachment, status, manager_status, hr_status, current_stage, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("LV-2026-0001", site_engineer_id, 1, "2026-03-25", "2026-03-27", 3, "Family commitment", None, "Pending HR Approval", "Approved", "Pending", "hr_review", created_at),
    )
    leave_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db.executemany(
        "INSERT INTO leave_history(leave_application_id, action, remarks, action_by, action_at) VALUES (?, ?, ?, ?, ?)",
        [
            (leave_id, "Submitted", "Employee submitted application", site_engineer_id, created_at),
            (leave_id, "Manager Approved", "Approved by Muhammad Waseem", site_manager_id, created_at),
        ],
    )

    attendance_seed_users = list(user_id_by_email.values())
    for uid in attendance_seed_users:
        for day_offset in range(0, 10):
            dt = date.today() - timedelta(days=day_offset)
            if dt.weekday() >= 5:
                status = "Weekend"
                cin = None
                cout = None
                hours = 0
            else:
                status = "Present"
                cin = "08:00"
                cout = "17:00"
                hours = 8
            db.execute(
                "INSERT INTO attendance(user_id, attendance_date, check_in, check_out, status, hours_worked, remarks) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, dt.isoformat(), cin, cout, status, hours, None),
            )

    payslips = [
        (site_manager_id, "Feb 2026", 9500, 1500, 300, 10700),
        (site_engineer_id, "Feb 2026", 4500, 500, 100, 4900),
        (user_id_by_email["faisal.malik@example.com"], "Feb 2026", 6000, 800, 150, 6650),
        (user_id_by_email["mirza.samiulla@example.com"], "Feb 2026", 6200, 900, 200, 6900),
        (user_id_by_email["salahudddin@example.com"], "Feb 2026", 5800, 700, 120, 6380),
    ]
    for row in payslips:
        db.execute(
            "INSERT INTO payroll_slips(user_id, month_label, basic_salary, allowances, deductions, net_salary, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*row, created_at),
        )

    db.executemany(
        "INSERT INTO audit_logs(actor_user_id, target_user_id, module_name, action_name, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (admin_id, site_engineer_id, "Payroll", "Seeded", "Created demo payroll slips", created_at),
            (hr_id, site_engineer_id, "Leave", "Reviewed", "HR review pending for demo request", created_at),
        ],
    )

    notifications = [
        (site_engineer_id, "Leave request update", "Your annual leave request is pending HR review.", "/leaves", 0, created_at),
        (site_manager_id, "Team leave awaiting attention", "A leave request has already moved to HR stage.", "/leaves", 0, created_at),
        (admin_id, "System ready", "Demo data was created successfully.", "/settings", 0, created_at),
    ]
    db.executemany("INSERT INTO notifications(user_id, title, message, link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?)", notifications)
    email_seed = [
        (site_engineer_id, "employee@example.com", "Leave request received", "Your leave request LV-2026-0001 is now pending HR review.", "Queued", created_at),
        (admin_id, "admin@example.com", "Demo portal initialized", "The portal database and demo users are ready.", "Queued", created_at),
    ]
    db.executemany("INSERT INTO email_queue(to_user_id, to_email, subject, body, status, created_at) VALUES (?, ?, ?, ?, ?, ?)", email_seed)

    db.execute("UPDATE users SET monthly_basic=?, default_allowances=?, deduction_per_absent=?, deduction_per_late=? WHERE email=?", (9500, 1500, 316.67, 50, "manager@example.com"))
    db.execute("UPDATE users SET monthly_basic=?, default_allowances=?, deduction_per_absent=?, deduction_per_late=? WHERE employee_code=?", (9500, 1500, 316.67, 50, "PAC-249"))
    db.commit()
    db.close()


@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user() else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        if not identifier or not password:
            flash("Please enter both your email or employee code and password.", "danger")
            return render_template("login.html")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?) OR upper(employee_code) = upper(?)",
            (identifier, identifier),
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password) and user["is_active"]:
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['full_name']}.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid login credentials.", "danger")
    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
        if user and user["is_active"]:
            temp_password = f"Temp@{secrets.token_hex(4)}A1"
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(temp_password), user["id"]))
            db.execute(
                "INSERT INTO email_queue(to_user_id, to_email, subject, body, status, created_at) VALUES (?, ?, ?, ?, 'Queued', ?)",
                (user["id"], user["email"], "Password reset", f"Your temporary password is: {temp_password}\nPlease log in and change it immediately.", now_str()),
            )
            db.execute(
                "INSERT INTO notifications(user_id, title, message, link, is_read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (user["id"], "Password reset requested", "A temporary password has been generated and queued in the email center.", url_for("change_password"), now_str()),
            )
            db.commit()
        flash("If that email exists, a temporary password has been generated and queued.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    db = get_db()
    counts = app_counts(user)
    if user["role"] == "employee":
        recent_leaves = db.execute("SELECT la.*, lt.name AS leave_type_name FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id WHERE la.user_id=? ORDER BY la.id DESC LIMIT 5", (user["id"],)).fetchall()
    elif user["role"] == "manager":
        recent_leaves = db.execute("SELECT la.*, lt.name AS leave_type_name, u.full_name FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id JOIN users u ON la.user_id=u.id WHERE u.manager_id=? ORDER BY la.id DESC LIMIT 8", (user["id"],)).fetchall()
    else:
        recent_leaves = db.execute("SELECT la.*, lt.name AS leave_type_name, u.full_name FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id JOIN users u ON la.user_id=u.id ORDER BY la.id DESC LIMIT 8").fetchall()
    recent_attendance = db.execute("SELECT * FROM attendance WHERE user_id=? ORDER BY attendance_date DESC LIMIT 5", (user["id"],)).fetchall()
    recent_notifications = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 5", (user["id"],)).fetchall()
    return render_template("dashboard.html", counts=counts, recent_leaves=recent_leaves, recent_attendance=recent_attendance, recent_notifications=recent_notifications)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    db = get_db()
    if request.method == "POST":
        phone = (request.form.get("phone") or "").strip()
        address = (request.form.get("address") or "").strip()
        emergency_contact = (request.form.get("emergency_contact") or "").strip()
        avatar = request.files.get("profile_picture")
        avatar_filename = user["avatar_filename"]
        if avatar and avatar.filename:
            if allowed_image_file(avatar.filename):
                ext = avatar.filename.rsplit('.', 1)[1].lower()
                avatar_filename = secure_filename(f"avatar_{user['id']}_{secrets.token_hex(8)}.{ext}")
                avatar_path = UPLOAD_FOLDER / avatar_filename
                avatar.save(avatar_path)
                old_avatar = user["avatar_filename"]
                if old_avatar and old_avatar != avatar_filename:
                    old_path = UPLOAD_FOLDER / old_avatar
                    if old_path.exists():
                        try:
                            old_path.unlink()
                        except OSError:
                            pass
            else:
                flash("Profile picture must be a PNG, JPG, or JPEG image.", "warning")
                return redirect(url_for("profile"))
        db.execute(
            "UPDATE users SET phone=?, address=?, emergency_contact=?, avatar_filename=? WHERE id=?",
            (phone, address, emergency_contact, avatar_filename, user["id"]),
        )
        log_audit("Profile", "Updated", "Updated own contact details", user["id"])
        db.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for("profile"))
    balances = db.execute("SELECT lb.*, lt.name AS leave_type_name FROM leave_balances lb JOIN leave_types lt ON lb.leave_type_id=lt.id WHERE lb.user_id=? ORDER BY lt.name", (user["id"],)).fetchall()
    return render_template("profile.html", balances=balances)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()
    db = get_db()
    if request.method == "POST":
        current_password = request.form["current_password"]
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]
        if not check_password_hash(user["password_hash"], current_password):
            flash("Current password is not correct.", "danger")
        elif len(new_password) < 8:
            flash("New password must be at least 8 characters.", "danger")
        elif new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user["id"]))
            log_audit("Security", "Password Changed", "Changed own password", user["id"])
            db.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


@app.route("/leave/apply", methods=["GET", "POST"])
@login_required
@role_required("employee", "manager", "hr")
def apply_leave():
    db = get_db()
    user = current_user()
    leave_types = db.execute("SELECT * FROM leave_types ORDER BY name").fetchall()
    if request.method == "POST":
        leave_type_id = int(request.form["leave_type_id"])
        from_date = request.form["from_date"]
        to_date = request.form["to_date"]
        reason = request.form["reason"].strip()
        start_date = datetime.strptime(from_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(to_date, "%Y-%m-%d").date()
        if end_date < start_date:
            flash("To date cannot be before from date.", "danger")
            return render_template("apply_leave.html", leave_types=leave_types)
        total_days = (end_date - start_date).days + 1
        attachment = request.files.get("attachment")
        attachment_name = None
        if attachment and attachment.filename:
            if not allowed_file(attachment.filename):
                flash("Attachment file type is not allowed.", "warning")
                return render_template("apply_leave.html", leave_types=leave_types)
            attachment_name = f"leave_{user['id']}_{secrets.token_hex(8)}_{secure_filename(attachment.filename)}"
            attachment.save(UPLOAD_FOLDER / attachment_name)
        next_num = db.execute("SELECT COUNT(*) AS c FROM leave_applications").fetchone()["c"] + 1
        app_no = f"LV-{date.today().year}-{next_num:04d}"
        db.execute(
            "INSERT INTO leave_applications(application_no, user_id, leave_type_id, from_date, to_date, total_days, reason, attachment, status, manager_status, hr_status, current_stage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (app_no, user["id"], leave_type_id, from_date, to_date, total_days, reason, attachment_name, "Draft Routing", "Pending", "Pending", "routing", now_str()),
        )
        leave_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute("INSERT INTO leave_history(leave_application_id, action, remarks, action_by, action_at) VALUES (?, ?, ?, ?, ?)", (leave_id, "Submitted", "Employee submitted leave request", user["id"], now_str()))
        steps = create_leave_approval_route(leave_id, user)
        if not steps:
            db.execute("DELETE FROM leave_applications WHERE id=?", (leave_id,))
            db.execute("DELETE FROM leave_history WHERE leave_application_id=?", (leave_id,))
            db.commit()
            flash("No approval route could be created for this employee. Please assign the required approvers first.", "danger")
            return render_template("apply_leave.html", leave_types=leave_types)
        refresh_leave_status(leave_id)
        pending_step = get_pending_leave_step(leave_id)
        if pending_step:
            approver = db.execute("SELECT full_name FROM users WHERE id=?", (pending_step["approver_user_id"],)).fetchone()
            notify_user(
                pending_step["approver_user_id"],
                "New leave request",
                f"{user['full_name']} submitted leave request {app_no} for your approval.",
                link=url_for("leave_detail", leave_id=leave_id),
                email_subject="New leave request awaiting approval",
                email_body=f"A new leave request from {user['full_name']} is waiting for your action.",
            )
        notify_user(user["id"], "Leave submitted", f"Your leave request {app_no} was submitted successfully.", url_for("my_leaves"))
        log_audit("Leave", "Submitted", f"Leave request {app_no} submitted", user["id"])
        db.commit()
        flash("Leave request submitted successfully.", "success")
        return redirect(url_for("my_leaves"))
    return render_template("apply_leave.html", leave_types=leave_types)


@app.route("/leaves")
@login_required
def my_leaves():
    db = get_db()
    user = current_user()
    base_query = "SELECT DISTINCT la.*, lt.name AS leave_type_name, u.full_name FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id JOIN users u ON la.user_id=u.id"
    params: list[Any] = []
    conditions: list[str] = []
    if user["role"] == "employee":
        conditions.append("la.user_id=?")
        params.append(user["id"])
    elif user["role"] == "manager":
        conditions.append("(la.user_id=? OR EXISTS (SELECT 1 FROM leave_approval_steps s WHERE s.leave_application_id=la.id AND s.approver_user_id=?))")
        params.extend([user["id"], user["id"]])
    elif user["role"] == "hr":
        conditions.append("(la.user_id=? OR EXISTS (SELECT 1 FROM leave_approval_steps s WHERE s.leave_application_id=la.id AND s.approver_user_id=?))")
        params.extend([user["id"], user["id"]])
    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)
    base_query += " ORDER BY la.id DESC"
    leaves = db.execute(base_query, params).fetchall()
    return render_template("leaves.html", leaves=leaves)


@app.route("/leave/<int:leave_id>", methods=["GET", "POST"])
@login_required
def leave_detail(leave_id: int):
    db = get_db()
    user = current_user()
    leave = db.execute(
        "SELECT la.*, lt.name AS leave_type_name, u.full_name, u.manager_id, u.department_id, d.name AS department_name, ds.name AS designation_name FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id JOIN users u ON la.user_id=u.id LEFT JOIN departments d ON u.department_id=d.id LEFT JOIN designations ds ON u.designation_id=ds.id WHERE la.id=?",
        (leave_id,),
    ).fetchone()
    if not leave:
        flash("Leave application not found.", "danger")
        return redirect(url_for("my_leaves"))
    pending_step = get_pending_leave_step(leave_id)
    can_view = user["role"] == "admin" or leave["user_id"] == user["id"] or db.execute("SELECT 1 FROM leave_approval_steps WHERE leave_application_id=? AND approver_user_id=?", (leave_id, user["id"])).fetchone() is not None
    if not can_view:
        flash("You do not have access to this leave request.", "danger")
        return redirect(url_for("my_leaves"))
    can_action = False
    current_action_label = None
    if pending_step and (user["role"] == "admin" or pending_step["approver_user_id"] == user["id"]):
        can_action = True
        current_action_label = pending_step["approver_title"]
    if request.method == "POST" and can_action:
        action = request.form["action"]
        remarks = request.form.get("remarks", "").strip()
        step = get_pending_leave_step(leave_id)
        if not step:
            flash("This leave request has already been actioned.", "warning")
            return redirect(url_for("leave_detail", leave_id=leave_id))
        hist_action = f"{step['approver_title']} {'Approved' if action == 'approve' else 'Rejected'}"
        if action == "approve":
            db.execute("UPDATE leave_approval_steps SET status='Approved', remarks=?, action_at=? WHERE id=?", (remarks, now_str(), step["id"]))
            next_step = db.execute("SELECT * FROM leave_approval_steps WHERE leave_application_id=? AND step_no>? ORDER BY step_no LIMIT 1", (leave_id, step["step_no"])).fetchone()
            if next_step:
                db.execute("UPDATE leave_approval_steps SET status='Pending' WHERE id=?", (next_step["id"],))
                approver = db.execute("SELECT full_name FROM users WHERE id=?", (next_step["approver_user_id"],)).fetchone()
                notify_user(next_step["approver_user_id"], "Leave request pending your review", f"{leave['application_no']} is waiting for your approval.", url_for("leave_detail", leave_id=leave_id))
                refresh_leave_status(leave_id)
                notify_user(leave["user_id"], f"{step['approver_title']} approved leave", f"{leave['application_no']} moved to the next approval stage.", url_for("leave_detail", leave_id=leave_id))
            else:
                db.execute("UPDATE leave_balances SET used_days=used_days+?, remaining_days=remaining_days-? WHERE user_id=? AND leave_type_id=?", (leave["total_days"], leave["total_days"], leave["user_id"], leave["leave_type_id"]))
                refresh_leave_status(leave_id)
                notify_user(leave["user_id"], "Leave approved", f"{leave['application_no']} was finally approved.", url_for("leave_detail", leave_id=leave_id))
        else:
            db.execute("UPDATE leave_approval_steps SET status='Rejected', remarks=?, action_at=? WHERE id=?", (remarks, now_str(), step["id"]))
            db.execute("UPDATE leave_approval_steps SET status='Cancelled' WHERE leave_application_id=? AND step_no>? AND status='Waiting'", (leave_id, step["step_no"]))
            db.execute("UPDATE leave_applications SET status=?, manager_status='Rejected', hr_status='Rejected', current_stage='closed' WHERE id=?", (f"Rejected by {step['approver_title']}", leave_id))
            notify_user(leave["user_id"], "Leave rejected", f"{leave['application_no']} was rejected by {step['approver_title']}.", url_for("leave_detail", leave_id=leave_id))
        db.execute("INSERT INTO leave_history(leave_application_id, action, remarks, action_by, action_at) VALUES (?, ?, ?, ?, ?)", (leave_id, hist_action, remarks, user["id"], now_str()))
        log_audit("Leave", hist_action, f"Leave request {leave['application_no']} actioned", leave["user_id"])
        db.commit()
        flash("Leave application updated successfully.", "success")
        return redirect(url_for("leave_detail", leave_id=leave_id))
    history = db.execute("SELECT lh.*, u.full_name FROM leave_history lh LEFT JOIN users u ON lh.action_by=u.id WHERE lh.leave_application_id=? ORDER BY lh.id ASC", (leave_id,)).fetchall()
    approval_steps = db.execute("SELECT s.*, u.full_name FROM leave_approval_steps s LEFT JOIN users u ON s.approver_user_id=u.id WHERE s.leave_application_id=? ORDER BY s.step_no", (leave_id,)).fetchall()
    return render_template("leave_detail.html", leave=leave, history=history, approval_steps=approval_steps, can_action=can_action, current_action_label=current_action_label)



@app.route("/team")
@login_required
def team():
    user = current_user()
    db = get_db()
    search = (request.args.get("q") or "").strip()
    query = """
        SELECT u.*, d.name AS department_name, ds.name AS designation_name
        FROM users u
        LEFT JOIN departments d ON u.department_id = d.id
        LEFT JOIN designations ds ON u.designation_id = ds.id
    """
    conditions: list[str] = []
    params: list[Any] = []
    if user["role"] == "manager":
        conditions.append("u.manager_id = ?")
        params.append(user["id"])
    if search:
        conditions.append("(u.full_name LIKE ? OR u.employee_code LIKE ? OR u.email LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY u.full_name"
    employees = db.execute(query, tuple(params)).fetchall()
    return render_template("team.html", employees=employees, search=search)


@app.route("/employees/<int:user_id>")
@login_required
def employee_detail(user_id: int):
    viewer = current_user()
    db = get_db()
    employee = db.execute(
        "SELECT u.*, d.name AS department_name, ds.name AS designation_name, m.full_name AS manager_name FROM users u LEFT JOIN departments d ON u.department_id=d.id LEFT JOIN designations ds ON u.designation_id=ds.id LEFT JOIN users m ON u.manager_id=m.id WHERE u.id=?",
        (user_id,),
    ).fetchone()
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("team"))
    can_view = viewer["role"] in {"hr", "admin"} or viewer["id"] == user_id or (viewer["role"] == "manager" and employee["manager_id"] == viewer["id"])
    if not can_view:
        flash("You do not have access to this employee.", "danger")
        return redirect(url_for("dashboard"))
    balances = db.execute("SELECT lb.*, lt.name AS leave_type_name FROM leave_balances lb JOIN leave_types lt ON lb.leave_type_id=lt.id WHERE lb.user_id=? ORDER BY lt.name", (user_id,)).fetchall()
    attendance_rows = db.execute("SELECT * FROM attendance WHERE user_id=? ORDER BY attendance_date DESC LIMIT 10", (user_id,)).fetchall()
    slips = db.execute("SELECT * FROM payroll_slips WHERE user_id=? ORDER BY id DESC LIMIT 6", (user_id,)).fetchall()
    docs = db.execute("SELECT * FROM employee_documents WHERE user_id=? ORDER BY id DESC LIMIT 6", (user_id,)).fetchall()
    return render_template("employee_detail.html", employee=employee, balances=balances, attendance_rows=attendance_rows, slips=slips, docs=docs)


@app.route("/employees/bulk-upload", methods=["GET", "POST"])
@login_required
@role_required("hr", "admin")
def bulk_employee_upload():
    db = get_db()
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Please choose an Excel file first.", "warning")
            return redirect(url_for("bulk_employee_upload"))
        ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if ext not in {"xlsx", "xlsm", "xltx", "xltm"}:
            flash("Bulk employee upload supports Excel .xlsx files only.", "warning")
            return redirect(url_for("bulk_employee_upload"))
        try:
            wb = load_workbook(filename=io.BytesIO(upload.read()), data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                flash("The uploaded Excel file is empty.", "warning")
                return redirect(url_for("bulk_employee_upload"))
            headers = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
            required = ["full_name", "email", "employee_code", "password", "role", "department", "designation"]
            missing = [c for c in required if c not in headers]
            if missing:
                flash(f"Missing required columns: {', '.join(missing)}", "danger")
                return redirect(url_for("bulk_employee_upload"))
            idx = {name: headers.index(name) for name in headers if name}

            def cell(row, key, default=""):
                pos = idx.get(key)
                if pos is None or pos >= len(row) or row[pos] is None:
                    return default
                return str(row[pos]).strip()

            created = 0
            skipped = 0
            for row in rows[1:]:
                if not row or not any(v is not None and str(v).strip() for v in row):
                    continue
                full_name = cell(row, "full_name")
                email = cell(row, "email").lower()
                employee_code = cell(row, "employee_code")
                password = cell(row, "password")
                role = cell(row, "role", "employee").lower()
                department_name = cell(row, "department")
                designation_name = cell(row, "designation")
                if not full_name or not email or not employee_code or not password or role not in get_role_options() or not department_name or not designation_name:
                    skipped += 1
                    continue
                existing = db.execute("SELECT id FROM users WHERE lower(email)=? OR employee_code=?", (email, employee_code)).fetchone()
                if existing:
                    skipped += 1
                    continue
                dept = db.execute("SELECT id FROM departments WHERE lower(name)=lower(?)", (department_name,)).fetchone()
                if not dept:
                    db.execute("INSERT INTO departments(name) VALUES (?)", (department_name,))
                    dept_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                else:
                    dept_id = dept["id"]
                desig = db.execute("SELECT id FROM designations WHERE lower(name)=lower(?)", (designation_name,)).fetchone()
                if not desig:
                    db.execute("INSERT INTO designations(name) VALUES (?)", (designation_name,))
                    desig_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                else:
                    desig_id = desig["id"]
                manager_ref = cell(row, "manager_email") or cell(row, "manager_code")
                manager_id = None
                if manager_ref:
                    manager = db.execute("SELECT id FROM users WHERE lower(email)=lower(?) OR employee_code=?", (manager_ref, manager_ref)).fetchone()
                    manager_id = manager["id"] if manager else None
                phone = cell(row, "phone")
                address = cell(row, "address")
                emergency_contact = cell(row, "emergency_contact")
                join_date = cell(row, "join_date", date.today().isoformat())
                monthly_basic = float(cell(row, "monthly_basic", "0") or 0)
                default_allowances = float(cell(row, "default_allowances", "0") or 0)
                deduction_per_absent = float(cell(row, "deduction_per_absent", "0") or 0)
                deduction_per_late = float(cell(row, "deduction_per_late", "0") or 0)
                is_active = 0 if cell(row, "is_active", "1").lower() in {"0", "false", "no", "inactive"} else 1
                db.execute(
                    "INSERT INTO users(full_name, email, employee_code, password_hash, role, department_id, designation_id, manager_id, phone, address, emergency_contact, join_date, monthly_basic, default_allowances, deduction_per_absent, deduction_per_late, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (full_name, email, employee_code, generate_password_hash(password), role, dept_id, desig_id, manager_id, phone, address, emergency_contact, join_date, monthly_basic, default_allowances, deduction_per_absent, deduction_per_late, is_active),
                )
                new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                for lt in db.execute("SELECT id, annual_quota FROM leave_types").fetchall():
                    db.execute("INSERT INTO leave_balances(user_id, leave_type_id, total_days, used_days, remaining_days) VALUES (?, ?, ?, 0, ?)", (new_id, lt["id"], lt["annual_quota"], lt["annual_quota"]))
                created += 1
            db.commit()
            flash(f"Bulk upload complete. Created {created} employee(s), skipped {skipped} row(s).", "success")
            return redirect(url_for("team"))
        except Exception as exc:
            flash(f"Could not process Excel file: {exc}", "danger")
            return redirect(url_for("bulk_employee_upload"))
    sample_headers = ["full_name", "email", "employee_code", "password", "role", "department", "designation", "manager_email", "phone", "address", "emergency_contact", "join_date", "monthly_basic", "default_allowances", "deduction_per_absent", "deduction_per_late", "is_active"]
    return render_template("employee_bulk_upload.html", sample_headers=sample_headers)


@app.route("/employees/new", methods=["GET", "POST"])
@login_required
@role_required("hr", "admin")
def new_employee():
    return employee_form_handler()


@app.route("/employees/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("hr", "admin")
def edit_employee(user_id: int):
    return employee_form_handler(user_id)


def employee_form_handler(user_id: int | None = None):
    db = get_db()
    departments = db.execute("SELECT * FROM departments ORDER BY name").fetchall()
    designations = db.execute("SELECT * FROM designations ORDER BY name").fetchall()
    managers = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    employee = None
    if user_id is not None:
        employee = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not employee:
            flash("Employee not found.", "danger")
            return redirect(url_for("team"))
    if request.method == "POST":
        form = request.form
        manager_id = form.get("manager_id") or None
        if user_id is None:
            db.execute(
                "INSERT INTO users(full_name, email, employee_code, password_hash, role, department_id, designation_id, manager_id, phone, address, emergency_contact, join_date, monthly_basic, default_allowances, deduction_per_absent, deduction_per_late, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (form["full_name"].strip(), form["email"].strip(), form["employee_code"].strip(), generate_password_hash(form["password"]), form["role"], form["department_id"], form["designation_id"], manager_id, form["phone"].strip(), form["address"].strip(), form["emergency_contact"].strip(), form["join_date"], float(form.get("monthly_basic") or 0), float(form.get("default_allowances") or 0), float(form.get("deduction_per_absent") or 0), float(form.get("deduction_per_late") or 0), 1 if form.get("is_active", "1") == "1" else 0),
            )
            new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            for lt in db.execute("SELECT id, annual_quota FROM leave_types").fetchall():
                db.execute("INSERT INTO leave_balances(user_id, leave_type_id, total_days, used_days, remaining_days) VALUES (?, ?, ?, 0, ?)", (new_id, lt["id"], lt["annual_quota"], lt["annual_quota"]))
            notify_user(new_id, "Account created", "Your employee portal account has been created.", url_for("profile"), "Employee portal account created", f"Welcome to the employee portal. Login email: {form['email'].strip()}")
            log_audit("Employee", "Created", f"Created employee {form['employee_code']}", new_id)
            db.commit()
            flash("Employee created successfully.", "success")
            return redirect(url_for("employee_detail", user_id=new_id))
        db.execute(
            "UPDATE users SET full_name=?, email=?, employee_code=?, role=?, department_id=?, designation_id=?, manager_id=?, phone=?, address=?, emergency_contact=?, join_date=?, monthly_basic=?, default_allowances=?, deduction_per_absent=?, deduction_per_late=?, is_active=? WHERE id=?",
            (form["full_name"].strip(), form["email"].strip(), form["employee_code"].strip(), form["role"], form["department_id"], form["designation_id"], manager_id, form["phone"].strip(), form["address"].strip(), form["emergency_contact"].strip(), form["join_date"], float(form.get("monthly_basic") or 0), float(form.get("default_allowances") or 0), float(form.get("deduction_per_absent") or 0), float(form.get("deduction_per_late") or 0), 1 if form.get("is_active", "1") == "1" else 0, user_id),
        )
        notify_user(user_id, "Profile updated", "Your employee profile details were updated by HR/Admin.", url_for("employee_detail", user_id=user_id))
        log_audit("Employee", "Updated", f"Updated employee {form['employee_code']}", user_id)
        db.commit()
        flash("Employee updated successfully.", "success")
        return redirect(url_for("employee_detail", user_id=user_id))
    return render_template("employee_form.html", departments=departments, designations=designations, managers=managers, employee=employee, role_options=get_role_options())


@app.route("/employees/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_employee(user_id: int):
    db = get_db()
    employee = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("team"))
    if user_id == current_user()["id"]:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("employee_detail", user_id=user_id))
    if employee["role"] == "admin":
        active_admins = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND is_active=1").fetchone()["c"]
        if active_admins <= 1:
            flash("You cannot deactivate the last active admin.", "danger")
            return redirect(url_for("employee_detail", user_id=user_id))
    db.execute("UPDATE users SET is_active=0, manager_id=NULL WHERE id=?", (user_id,))
    db.execute("UPDATE users SET manager_id=NULL WHERE manager_id=?", (user_id,))
    notify_user(user_id, "Account deactivated", "Your employee portal access has been deactivated.", None)
    log_audit("Employee", "Deactivated", f"Deactivated employee {employee['employee_code']}", user_id)
    db.commit()
    flash("Employee deactivated successfully.", "success")
    return redirect(url_for("team"))


@app.route("/employees/<int:user_id>/hard-delete", methods=["POST"])
@login_required
@role_required("admin")
def hard_delete_employee(user_id: int):
    db = get_db()
    employee = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("team"))
    if user_id == current_user()["id"]:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("employee_detail", user_id=user_id))
    if employee["role"] == "admin":
        active_admins = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND is_active=1").fetchone()["c"]
        if active_admins <= 1:
            flash("You cannot delete the last active admin.", "danger")
            return redirect(url_for("employee_detail", user_id=user_id))
    db.execute("UPDATE users SET manager_id=NULL WHERE manager_id=?", (user_id,))
    db.execute("DELETE FROM leave_approval_steps WHERE leave_application_id IN (SELECT id FROM leave_applications WHERE user_id=?) OR approver_user_id=?", (user_id, user_id))
    db.execute("DELETE FROM leave_history WHERE leave_application_id IN (SELECT id FROM leave_applications WHERE user_id=?) OR action_by=?", (user_id, user_id))
    db.execute("DELETE FROM leave_applications WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM leave_balances WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM employee_documents WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM attendance WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM payroll_slips WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM notifications WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM email_queue WHERE to_user_id=?", (user_id,))
    db.execute("DELETE FROM audit_logs WHERE actor_user_id=? OR target_user_id=?", (user_id, user_id))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    log_audit("Employee", "Deleted", f"Permanently deleted employee {employee['employee_code']}")
    db.commit()
    flash("Employee permanently deleted successfully.", "success")
    return redirect(url_for("team"))


@app.route("/employees/<int:user_id>/reset-password", methods=["POST"])
@login_required
@role_required("admin", "hr")
def reset_employee_password(user_id: int):
    db = get_db()
    employee = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("team"))
    temp_password = f"Reset@{secrets.token_hex(4)}A1"
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(temp_password), user_id))
    notify_user(
        user_id,
        "Password reset",
        "HR/Admin generated a temporary password for your account.",
        url_for("change_password"),
        "Password reset",
        f"Your temporary password is: {temp_password}\nPlease sign in and change it immediately.",
    )
    log_audit("Security", "Password Reset", "Temporary password generated by HR/Admin", user_id)
    db.commit()
    flash("Temporary password generated and queued in the email center.", "success")
    return redirect(url_for("employee_detail", user_id=user_id))


@app.route("/attendance")
@login_required
def attendance_view():
    user = current_user()
    db = get_db()
    selected_user_id = request.args.get("user_id", type=int) or user["id"]
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    if user["role"] == "employee":
        selected_user_id = user["id"]
    elif user["role"] == "manager":
        allowed = db.execute("SELECT COUNT(*) AS c FROM users WHERE id=? AND manager_id=?", (selected_user_id, user["id"])).fetchone()["c"]
        if selected_user_id != user["id"] and not allowed:
            selected_user_id = user["id"]
    rows = db.execute(
        "SELECT a.*, hc.title AS holiday_title, hc.holiday_type FROM attendance a LEFT JOIN holiday_calendar hc ON a.attendance_date=hc.holiday_date WHERE a.user_id=? AND substr(a.attendance_date,1,7)=? ORDER BY a.attendance_date DESC",
        (selected_user_id, month),
    ).fetchall()
    employees = []
    if user["role"] in {"manager", "hr", "admin"}:
        if user["role"] == "manager":
            employees = db.execute("SELECT id, full_name FROM users WHERE manager_id=? AND is_active=1 ORDER BY full_name", (user["id"],)).fetchall()
        else:
            employees = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    summary = {
        "present": sum(1 for r in rows if r["status"] == "Present"),
        "absent": sum(1 for r in rows if r["status"] == "Absent"),
        "late": sum(1 for r in rows if r["status"] == "Late"),
        "hours": round(sum(r["hours_worked"] for r in rows), 1),
        "ot_hours": round(sum((r["ot_hours"] or 0) for r in rows), 1),
    }
    selected_employee = db.execute("SELECT id, full_name FROM users WHERE id=?", (selected_user_id,)).fetchone()
    holiday_rows = db.execute("SELECT * FROM holiday_calendar WHERE substr(holiday_date,1,7)=? ORDER BY holiday_date", (month,)).fetchall()
    return render_template("attendance.html", rows=rows, employees=employees, selected_user_id=selected_user_id, month=month, summary=summary, selected_employee=selected_employee, holiday_rows=holiday_rows)


@app.route("/payroll")
@login_required
def payroll_view():
    user = current_user()
    db = get_db()
    selected_user_id = request.args.get("user_id", type=int) or user["id"]
    if user["role"] == "employee":
        selected_user_id = user["id"]
    elif user["role"] == "manager":
        allowed = db.execute("SELECT COUNT(*) AS c FROM users WHERE id=? AND manager_id=?", (selected_user_id, user["id"])).fetchone()["c"]
        if selected_user_id != user["id"] and not allowed:
            selected_user_id = user["id"]
    slips = db.execute("SELECT * FROM payroll_slips WHERE user_id=? ORDER BY id DESC", (selected_user_id,)).fetchall()
    employees = []
    if user["role"] in {"manager", "hr", "admin"}:
        if user["role"] == "manager":
            employees = db.execute("SELECT id, full_name FROM users WHERE manager_id=? AND is_active=1 ORDER BY full_name", (user["id"],)).fetchall()
        else:
            employees = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    selected_employee = db.execute("SELECT id, full_name FROM users WHERE id=?", (selected_user_id,)).fetchone()
    return render_template("payroll.html", slips=slips, employees=employees, selected_user_id=selected_user_id, selected_employee=selected_employee)




@app.route("/attendance/add", methods=["GET", "POST"])
@login_required
@role_required("admin")
def attendance_add():
    db = get_db()
    employees = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    statuses = ["Present", "Absent", "Leave", "Late", "Half Day", "Holiday", "Vacation"]
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        attendance_date = (request.form.get("attendance_date") or "").strip()
        check_in = (request.form.get("check_in") or "").strip() or None
        check_out = (request.form.get("check_out") or "").strip() or None
        status = (request.form.get("status") or "Present").strip()
        remarks = (request.form.get("remarks") or "").strip() or None
        hours_worked = request.form.get("hours_worked", type=float)
        if hours_worked is None:
            hours_worked = calculate_hours(check_in, check_out)
        manual_ot = request.form.get("ot_hours", type=float)
        ot_hours = compute_ot_hours(db, attendance_date, status, hours_worked, manual_ot)
        if not user_id or not attendance_date:
            flash("Employee and date are required.", "danger")
        else:
            db.execute(
                "INSERT INTO attendance(user_id, attendance_date, check_in, check_out, status, hours_worked, ot_hours, remarks) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, attendance_date, check_in, check_out, status, hours_worked, ot_hours, remarks),
            )
            employee = db.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()
            log_audit("Attendance", "Added", f"Added attendance for {employee['full_name']} on {attendance_date}", user_id)
            db.commit()
            flash("Attendance record added.", "success")
            return redirect(url_for("attendance_view", user_id=user_id, month=attendance_date[:7]))
    return render_template("attendance_form.html", record=None, employees=employees, statuses=statuses)


@app.route("/attendance/edit/<int:attendance_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def attendance_edit(attendance_id: int):
    db = get_db()
    record = db.execute("SELECT * FROM attendance WHERE id=?", (attendance_id,)).fetchone()
    if not record:
        flash("Attendance record not found.", "danger")
        return redirect(url_for("attendance_view"))
    employees = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    statuses = ["Present", "Absent", "Leave", "Late", "Half Day", "Holiday", "Vacation"]
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        attendance_date = (request.form.get("attendance_date") or "").strip()
        check_in = (request.form.get("check_in") or "").strip() or None
        check_out = (request.form.get("check_out") or "").strip() or None
        status = (request.form.get("status") or "Present").strip()
        remarks = (request.form.get("remarks") or "").strip() or None
        hours_worked = request.form.get("hours_worked", type=float)
        if hours_worked is None:
            hours_worked = calculate_hours(check_in, check_out)
        manual_ot = request.form.get("ot_hours", type=float)
        ot_hours = compute_ot_hours(db, attendance_date, status, hours_worked, manual_ot)
        db.execute(
            "UPDATE attendance SET user_id=?, attendance_date=?, check_in=?, check_out=?, status=?, hours_worked=?, ot_hours=?, remarks=? WHERE id=?",
            (user_id, attendance_date, check_in, check_out, status, hours_worked, ot_hours, remarks, attendance_id),
        )
        employee = db.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()
        log_audit("Attendance", "Edited", f"Edited attendance for {employee['full_name']} on {attendance_date}", user_id)
        db.commit()
        flash("Attendance record updated.", "success")
        return redirect(url_for("attendance_view", user_id=user_id, month=attendance_date[:7]))
    return render_template("attendance_form.html", record=record, employees=employees, statuses=statuses)


@app.route("/attendance/delete/<int:attendance_id>", methods=["POST"])
@login_required
@role_required("admin")
def attendance_delete(attendance_id: int):
    db = get_db()
    record = db.execute("SELECT * FROM attendance WHERE id=?", (attendance_id,)).fetchone()
    if not record:
        flash("Attendance record not found.", "danger")
        return redirect(url_for("attendance_view"))
    db.execute("DELETE FROM attendance WHERE id=?", (attendance_id,))
    log_audit("Attendance", "Deleted", f"Deleted attendance entry on {record['attendance_date']}", record['user_id'])
    db.commit()
    flash("Attendance record deleted.", "success")
    return redirect(url_for("attendance_view", user_id=record['user_id'], month=str(record['attendance_date'])[:7]))


@app.route("/payroll/add", methods=["GET", "POST"])
@login_required
@role_required("admin")
def payroll_add():
    db = get_db()
    employees = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        month_label = (request.form.get("month_label") or "").strip()
        basic_salary = request.form.get("basic_salary", type=float) or 0.0
        allowances = request.form.get("allowances", type=float) or 0.0
        deductions = request.form.get("deductions", type=float) or 0.0
        net_salary = basic_salary + allowances - deductions
        if not user_id or not month_label:
            flash("Employee and month are required.", "danger")
        else:
            db.execute(
                "INSERT INTO payroll_slips(user_id, month_label, basic_salary, allowances, deductions, net_salary, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, month_label, basic_salary, allowances, deductions, net_salary, now_str()),
            )
            employee = db.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()
            log_audit("Payroll", "Added", f"Added payslip for {employee['full_name']} ({month_label})", user_id)
            db.commit()
            flash("Payslip added.", "success")
            return redirect(url_for("payroll_view", user_id=user_id))
    return render_template("payroll_form.html", slip=None, employees=employees)


@app.route("/payroll/edit/<int:slip_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def payroll_edit(slip_id: int):
    db = get_db()
    slip = db.execute("SELECT * FROM payroll_slips WHERE id=?", (slip_id,)).fetchone()
    if not slip:
        flash("Payslip not found.", "danger")
        return redirect(url_for("payroll_view"))
    employees = db.execute("SELECT id, full_name FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        month_label = (request.form.get("month_label") or "").strip()
        basic_salary = request.form.get("basic_salary", type=float) or 0.0
        allowances = request.form.get("allowances", type=float) or 0.0
        deductions = request.form.get("deductions", type=float) or 0.0
        net_salary = basic_salary + allowances - deductions
        db.execute(
            "UPDATE payroll_slips SET user_id=?, month_label=?, basic_salary=?, allowances=?, deductions=?, net_salary=? WHERE id=?",
            (user_id, month_label, basic_salary, allowances, deductions, net_salary, slip_id),
        )
        employee = db.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()
        log_audit("Payroll", "Edited", f"Edited payslip for {employee['full_name']} ({month_label})", user_id)
        db.commit()
        flash("Payslip updated.", "success")
        return redirect(url_for("payroll_view", user_id=user_id))
    return render_template("payroll_form.html", slip=slip, employees=employees)


@app.route("/payroll/delete/<int:slip_id>", methods=["POST"])
@login_required
@role_required("admin")
def payroll_delete(slip_id: int):
    db = get_db()
    slip = db.execute("SELECT * FROM payroll_slips WHERE id=?", (slip_id,)).fetchone()
    if not slip:
        flash("Payslip not found.", "danger")
        return redirect(url_for("payroll_view"))
    db.execute("DELETE FROM payroll_slips WHERE id=?", (slip_id,))
    log_audit("Payroll", "Deleted", f"Deleted payslip {slip['month_label']}", slip['user_id'])
    db.commit()
    flash("Payslip deleted.", "success")
    return redirect(url_for("payroll_view", user_id=slip['user_id']))

@app.route("/reports")
@login_required
@role_required("hr", "admin")
def reports():
    db = get_db()
    leave_summary = db.execute("SELECT status, COUNT(*) AS total FROM leave_applications GROUP BY status ORDER BY total DESC").fetchall()
    dept_summary = db.execute("SELECT d.name AS department_name, COUNT(u.id) AS total FROM departments d LEFT JOIN users u ON u.department_id=d.id AND u.is_active=1 GROUP BY d.id, d.name ORDER BY d.name").fetchall()
    attendance_summary = db.execute("SELECT status, COUNT(*) AS total FROM attendance GROUP BY status ORDER BY total DESC").fetchall()
    return render_template("reports.html", leave_summary=leave_summary, dept_summary=dept_summary, attendance_summary=attendance_summary)


@app.route("/documents")
@login_required
def documents():
    user = current_user()
    db = get_db()
    employees = []
    if user["role"] in {"hr", "admin"}:
        docs = db.execute("SELECT ed.*, u.full_name FROM employee_documents ed JOIN users u ON ed.user_id=u.id ORDER BY ed.id DESC").fetchall()
        employees = db.execute("SELECT id, full_name, employee_code FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    else:
        docs = db.execute("SELECT ed.*, u.full_name FROM employee_documents ed JOIN users u ON ed.user_id=u.id WHERE user_id=? ORDER BY ed.id DESC", (user["id"],)).fetchall()
    return render_template("documents.html", docs=docs, employees=employees)


@app.route("/documents/upload", methods=["POST"])
@login_required
@role_required("hr", "admin")
def upload_document():
    db = get_db()
    user_id = int(request.form["user_id"])
    title = request.form["title"].strip()
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose a file.", "danger")
        return redirect(url_for("documents"))
    if not allowed_file(file.filename):
        flash("Unsupported file type.", "danger")
        return redirect(url_for("documents"))
    filename = f"doc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    db.execute("INSERT INTO employee_documents(user_id, title, file_name, uploaded_at) VALUES (?, ?, ?, ?)", (user_id, title, filename, now_str()))
    notify_user(user_id, "New document uploaded", f"A new document titled '{title}' has been uploaded to your portal.", url_for("documents"))
    log_audit("Documents", "Uploaded", f"Uploaded document {title}", user_id)
    db.commit()
    flash("Document uploaded successfully.", "success")
    return redirect(url_for("documents"))


@app.route("/notifications")
@login_required
def notifications_view():
    db = get_db()
    user = current_user()
    mark_all = request.args.get("mark_all")
    if mark_all == "1":
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
        db.commit()
        flash("All notifications marked as read.", "success")
        return redirect(url_for("notifications_view"))
    notifications = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    return render_template("notifications.html", notifications=notifications)


@app.route("/notifications/<int:notification_id>/read")
@login_required
def mark_notification_read(notification_id: int):
    db = get_db()
    user = current_user()
    row = db.execute("SELECT * FROM notifications WHERE id=? AND user_id=?", (notification_id, user["id"])).fetchone()
    if row:
        db.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notification_id,))
        db.commit()
        if row["link"]:
            return redirect(row["link"])
    return redirect(url_for("notifications_view"))


@app.route("/email-center")
@login_required
@role_required("admin")
def email_center():
    emails = get_db().execute(
        "SELECT eq.*, u.full_name FROM email_queue eq LEFT JOIN users u ON eq.to_user_id=u.id ORDER BY eq.id DESC LIMIT 200"
    ).fetchall()
    return render_template("email_center.html", emails=emails)


@app.route("/email-center/<int:email_id>/sent", methods=["POST"])
@login_required
@role_required("admin")
def mark_email_sent(email_id: int):
    db = get_db()
    db.execute("UPDATE email_queue SET status='Sent' WHERE id=?", (email_id,))
    log_audit("Email", "Marked Sent", f"Marked email #{email_id} as sent")
    db.commit()
    flash("Email marked as sent.", "success")
    return redirect(url_for("email_center"))


@app.route("/payroll/bulk-upload", methods=["GET", "POST"])
@login_required
@role_required("admin")
def payroll_bulk_upload():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose an Excel file.", "danger")
            return redirect(url_for("payroll_bulk_upload"))
        try:
            wb = load_workbook(io.BytesIO(file.read()), data_only=True)
            ws = wb.active
            count = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0]:
                    continue
                employee_code = str(row[0]).strip()
                month_label = str(row[1]).strip()
                basic_salary = float(row[2] or 0)
                allowances = float(row[3] or 0)
                deductions = float(row[4] or 0)
                user = db.execute("SELECT id FROM users WHERE employee_code=?", (employee_code,)).fetchone()
                if not user:
                    continue
                net_salary = round(basic_salary + allowances - deductions, 2)
                existing = db.execute("SELECT id FROM payroll_slips WHERE user_id=? AND month_label=?", (user["id"], month_label)).fetchone()
                if existing:
                    db.execute("UPDATE payroll_slips SET basic_salary=?, allowances=?, deductions=?, net_salary=?, generated_at=? WHERE id=?", (basic_salary, allowances, deductions, net_salary, now_str(), existing["id"]))
                else:
                    db.execute("INSERT INTO payroll_slips(user_id, month_label, basic_salary, allowances, deductions, net_salary, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (user["id"], month_label, basic_salary, allowances, deductions, net_salary, now_str()))
                count += 1
            db.commit()
            flash(f"Payslips imported/updated for {count} rows.", "success")
            return redirect(url_for("payroll_view"))
        except Exception as exc:
            flash(f"Upload failed: {exc}", "danger")
    return render_template("payroll_bulk_upload.html")


@app.route("/payroll/generate-auto", methods=["GET", "POST"])
@login_required
@role_required("admin")
def payroll_generate_auto():
    db = get_db()
    employees = db.execute("SELECT id, full_name, employee_code FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    month = request.form.get("month") or date.today().strftime("%Y-%m")
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        if user_id:
            upsert_payroll_from_attendance(user_id, month)
        else:
            for emp in employees:
                upsert_payroll_from_attendance(emp["id"], month)
        db.commit()
        flash("Payslip generation completed.", "success")
        return redirect(url_for("payroll_view"))
    return render_template("payroll_generate_auto.html", employees=employees, month=month)


@app.route("/attendance/bulk-upload", methods=["GET", "POST"])
@login_required
@role_required("admin")
def attendance_bulk_upload():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose an Excel file.", "danger")
            return redirect(url_for("attendance_bulk_upload"))
        try:
            wb = load_workbook(io.BytesIO(file.read()), data_only=True)
            ws = wb.active
            count = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0] or not row[1]:
                    continue
                employee_code = str(row[0]).strip()
                attendance_date = str(row[1]).strip()
                check_in = str(row[2]).strip() if len(row) > 2 and row[2] not in (None, "") else None
                check_out = str(row[3]).strip() if len(row) > 3 and row[3] not in (None, "") else None
                status = str(row[4]).strip() if len(row) > 4 and row[4] not in (None, "") else "Present"
                remarks = str(row[5]).strip() if len(row) > 5 and row[5] not in (None, "") else None
                manual_ot = float(row[6]) if len(row) > 6 and row[6] not in (None, "") else None
                user = db.execute("SELECT id FROM users WHERE employee_code=?", (employee_code,)).fetchone()
                if not user:
                    continue
                hours_worked = calculate_hours(check_in, check_out)
                ot_hours = compute_ot_hours(db, attendance_date, status, hours_worked, manual_ot)
                existing = db.execute("SELECT id FROM attendance WHERE user_id=? AND attendance_date=?", (user["id"], attendance_date)).fetchone()
                if existing:
                    db.execute("UPDATE attendance SET check_in=?, check_out=?, status=?, hours_worked=?, ot_hours=?, remarks=? WHERE id=?", (check_in, check_out, status, hours_worked, ot_hours, remarks, existing["id"]))
                else:
                    db.execute("INSERT INTO attendance(user_id, attendance_date, check_in, check_out, status, hours_worked, ot_hours, remarks) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (user["id"], attendance_date, check_in, check_out, status, hours_worked, ot_hours, remarks))
                count += 1
            db.commit()
            flash(f"Attendance imported/updated for {count} rows.", "success")
            return redirect(url_for("attendance_view"))
        except Exception as exc:
            flash(f"Upload failed: {exc}", "danger")
    return render_template("attendance_bulk_upload.html")


@app.route("/attendance/monthly-editor", methods=["GET", "POST"])
@login_required
@role_required("admin")
def attendance_monthly_editor():
    db = get_db()
    attendance_date = request.values.get("attendance_date") or date.today().strftime("%Y-%m-%d")
    employees = db.execute("SELECT id, full_name, employee_code FROM users WHERE is_active=1 ORDER BY full_name").fetchall()
    statuses = ["Present", "Absent", "Leave", "Late", "Half Day", "Holiday", "Vacation"]
    existing_rows = {r["user_id"]: r for r in db.execute("SELECT * FROM attendance WHERE attendance_date=?", (attendance_date,)).fetchall()}
    if request.method == "POST":
        for emp in employees:
            prefix = f"emp_{emp['id']}_"
            status = request.form.get(prefix + "status", "Present")
            check_in = (request.form.get(prefix + "check_in") or "").strip() or None
            check_out = (request.form.get(prefix + "check_out") or "").strip() or None
            remarks = (request.form.get(prefix + "remarks") or "").strip() or None
            hours = calculate_hours(check_in, check_out)
            ot_hours = compute_ot_hours(db, attendance_date, status, hours)
            current = existing_rows.get(emp["id"])
            if current:
                db.execute("UPDATE attendance SET status=?, check_in=?, check_out=?, hours_worked=?, ot_hours=?, remarks=? WHERE id=?", (status, check_in, check_out, hours, ot_hours, remarks, current["id"]))
            else:
                db.execute("INSERT INTO attendance(user_id, attendance_date, check_in, check_out, status, hours_worked, ot_hours, remarks) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (emp["id"], attendance_date, check_in, check_out, status, hours, ot_hours, remarks))
        db.commit()
        flash("Attendance sheet saved successfully.", "success")
        return redirect(url_for("attendance_monthly_editor", attendance_date=attendance_date))
    holiday_row = get_holiday_row(db, attendance_date)
    return render_template("attendance_monthly_editor.html", employees=employees, existing=existing_rows, statuses=statuses, attendance_date=attendance_date, holiday_row=holiday_row)


@app.route("/masters", methods=["GET", "POST"])
@login_required
@role_required("admin")
def masters_view():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "department":
                name = (request.form.get("name") or "").strip()
                if name:
                    db.execute("INSERT INTO departments(name) VALUES (?)", (name,))
                    flash("Department added.", "success")
            elif action == "designation":
                name = (request.form.get("name") or "").strip()
                if name:
                    db.execute("INSERT INTO designations(name) VALUES (?)", (name,))
                    flash("Designation added.", "success")
            elif action == "promote_manager":
                user_id = request.form.get("user_id", type=int)
                if user_id:
                    db.execute("UPDATE users SET role='manager' WHERE id=?", (user_id,))
                    flash("User promoted to manager.", "success")
            db.commit()
        except sqlite3.IntegrityError:
            flash("This value already exists.", "warning")
        return redirect(url_for("masters_view"))
    departments = db.execute("SELECT * FROM departments ORDER BY name").fetchall()
    designations = db.execute("SELECT * FROM designations ORDER BY name").fetchall()
    managers = db.execute("SELECT id, full_name, employee_code FROM users WHERE role='manager' AND is_active=1 ORDER BY full_name").fetchall()
    non_managers = db.execute("SELECT id, full_name, employee_code FROM users WHERE role!='manager' AND is_active=1 ORDER BY full_name").fetchall()
    return render_template("masters.html", departments=departments, designations=designations, managers=managers, non_managers=non_managers, role_options=get_role_options())


@app.route("/calendar", methods=["GET", "POST"])
@login_required
@role_required("admin")
def calendar_view():
    db = get_db()
    if request.method == "POST":
        holiday_date = (request.form.get("holiday_date") or "").strip()
        title = (request.form.get("title") or "").strip()
        holiday_type = (request.form.get("holiday_type") or "Holiday").strip()
        if holiday_date and title:
            existing = db.execute("SELECT id FROM holiday_calendar WHERE holiday_date=?", (holiday_date,)).fetchone()
            if existing:
                db.execute("UPDATE holiday_calendar SET title=?, holiday_type=? WHERE id=?", (title, holiday_type, existing["id"]))
                flash("Calendar day updated.", "success")
            else:
                db.execute("INSERT INTO holiday_calendar(holiday_date, title, holiday_type, created_at) VALUES (?, ?, ?, ?)", (holiday_date, title, holiday_type, now_str()))
                flash("Calendar day added.", "success")
            db.commit()
        else:
            flash("Date and title are required.", "danger")
        return redirect(url_for("calendar_view"))
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    rows = db.execute("SELECT * FROM holiday_calendar WHERE substr(holiday_date,1,7)=? ORDER BY holiday_date", (month,)).fetchall()
    return render_template("calendar.html", rows=rows, month=month)


@app.route("/calendar/delete/<int:holiday_id>", methods=["POST"])
@login_required
@role_required("admin")
def calendar_delete(holiday_id: int):
    db = get_db()
    db.execute("DELETE FROM holiday_calendar WHERE id=?", (holiday_id,))
    db.commit()
    flash("Calendar day removed.", "success")
    return redirect(url_for("calendar_view"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def settings_view():
    db = get_db()
    current = db.execute("SELECT * FROM company_settings ORDER BY id DESC LIMIT 1").fetchone()
    if request.method == "POST":
        db.execute(
            "UPDATE company_settings SET company_name=?, leave_workflow=?, default_working_hours=?, allow_document_upload=? WHERE id=?",
            (request.form["company_name"].strip(), request.form["leave_workflow"].strip(), float(request.form["default_working_hours"]), 1 if request.form.get("allow_document_upload") else 0, current["id"]),
        )
        log_audit("Settings", "Updated", "Updated company settings")
        db.commit()
        flash("Settings updated successfully.", "success")
        return redirect(url_for("settings_view"))
    recent_logs = db.execute(
        "SELECT al.*, a.full_name AS actor_name, t.full_name AS target_name FROM audit_logs al LEFT JOIN users a ON al.actor_user_id=a.id LEFT JOIN users t ON al.target_user_id=t.id ORDER BY al.id DESC LIMIT 20"
    ).fetchall()
    return render_template("settings.html", setting=current, recent_logs=recent_logs)


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename: str):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/initdb")
def initialize_database():
    init_db()
    return "Database initialized successfully."


if __name__ == "__main__":
    if not DATABASE.exists():
        init_db()
    app.run(debug=True)
