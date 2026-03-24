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
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "employee_portal.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "doc", "docx", "xlsx"}

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-key"
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
UPLOAD_FOLDER.mkdir(exist_ok=True)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
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
        counts["manager_pending"] = db.execute("SELECT COUNT(*) AS c FROM leave_applications la JOIN users u ON la.user_id = u.id WHERE u.manager_id = ? AND la.manager_status='Pending'", (user["id"],)).fetchone()["c"]
    if user["role"] in {"hr", "admin"}:
        counts["total_employees"] = db.execute("SELECT COUNT(*) AS c FROM users WHERE is_active = 1").fetchone()["c"]
        counts["hr_pending"] = db.execute("SELECT COUNT(*) AS c FROM leave_applications WHERE manager_status='Approved' AND hr_status='Pending'").fetchone()["c"]
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
        ("Employee Portal Demo", "Site Engineer / Site Staff → Site Manager → HR Final Review", 8.0, 1),
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

    db.commit()
    db.close()


@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user() else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form["identifier"].strip()
        password = request.form["password"]
        user = get_db().execute("SELECT * FROM users WHERE email = ? OR employee_code = ?", (identifier, identifier)).fetchone()
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
        db.execute(
            "UPDATE users SET phone=?, address=?, emergency_contact=? WHERE id=?",
            (request.form["phone"].strip(), request.form["address"].strip(), request.form["emergency_contact"].strip(), user["id"]),
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
@role_required("employee")
def apply_leave():
    db = get_db()
    user = current_user()
    leave_types = db.execute("SELECT * FROM leave_types ORDER BY name").fetchall()
    if request.method == "POST":
        leave_type_id = int(request.form["leave_type_id"])
        from_date = request.form["from_date"]
        to_date = request.form["to_date"]
        reason = request.form["reason"].strip()
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
        total_days = (end - start).days + 1
        if total_days <= 0:
            flash("To date must be on or after from date.", "danger")
            return render_template("apply_leave.html", leave_types=leave_types)
        attachment_name = None
        file = request.files.get("attachment")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Unsupported file type.", "danger")
                return render_template("apply_leave.html", leave_types=leave_types)
            attachment_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], attachment_name))
        next_num = db.execute("SELECT COUNT(*) AS c FROM leave_applications").fetchone()["c"] + 1
        app_no = f"LV-2026-{next_num:04d}"
        db.execute(
            "INSERT INTO leave_applications(application_no, user_id, leave_type_id, from_date, to_date, total_days, reason, attachment, status, manager_status, hr_status, current_stage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (app_no, user["id"], leave_type_id, from_date, to_date, total_days, reason, attachment_name, "Pending Manager Approval", "Pending", "Pending", "manager_review", now_str()),
        )
        leave_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute("INSERT INTO leave_history(leave_application_id, action, remarks, action_by, action_at) VALUES (?, ?, ?, ?, ?)", (leave_id, "Submitted", "Employee submitted leave request", user["id"], now_str()))
        if user["manager_id"]:
            notify_user(
                user["manager_id"],
                "New leave request",
                f"{user['full_name']} submitted leave request {app_no}.",
                link=url_for("leave_detail", leave_id=leave_id),
                email_subject=f"Leave request {app_no}",
                email_body=f"A new leave request from {user['full_name']} requires your review.",
            )
        notify_user(user["id"], "Leave submitted", f"Your leave request {app_no} was submitted successfully.", url_for("my_leaves"))
        log_audit("Leave", "Submitted", f"Leave request {app_no} submitted", user["id"])
        db.commit()
        flash("Leave application submitted successfully.", "success")
        return redirect(url_for("my_leaves"))
    return render_template("apply_leave.html", leave_types=leave_types)


@app.route("/leaves")
@login_required
def my_leaves():
    user = current_user()
    db = get_db()
    query = "SELECT la.*, lt.name AS leave_type_name, u.full_name FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id JOIN users u ON la.user_id=u.id"
    params: tuple[Any, ...] = ()
    if user["role"] == "employee":
        query += " WHERE la.user_id=?"
        params = (user["id"],)
    elif user["role"] == "manager":
        query += " WHERE u.manager_id=?"
        params = (user["id"],)
    query += " ORDER BY la.id DESC"
    leaves = db.execute(query, params).fetchall()
    return render_template("leaves.html", leaves=leaves)


@app.route("/leave/<int:leave_id>", methods=["GET", "POST"])
@login_required
def leave_detail(leave_id: int):
    user = current_user()
    db = get_db()
    leave = db.execute(
        "SELECT la.*, lt.name AS leave_type_name, u.full_name, u.manager_id FROM leave_applications la JOIN leave_types lt ON la.leave_type_id=lt.id JOIN users u ON la.user_id=u.id WHERE la.id=?",
        (leave_id,),
    ).fetchone()
    if not leave:
        flash("Leave application not found.", "danger")
        return redirect(url_for("my_leaves"))
    can_view = user["role"] in {"hr", "admin"} or leave["user_id"] == user["id"] or (user["role"] == "manager" and leave["manager_id"] == user["id"])
    if not can_view:
        flash("You do not have access to this record.", "danger")
        return redirect(url_for("my_leaves"))
    if request.method == "POST":
        action = request.form["action"]
        remarks = request.form.get("remarks", "").strip() or None
        hist_action = None
        if user["role"] == "manager" and leave["manager_id"] == user["id"] and leave["manager_status"] == "Pending":
            if action == "approve":
                db.execute("UPDATE leave_applications SET manager_status='Approved', status='Pending HR Approval', current_stage='hr_review' WHERE id=?", (leave_id,))
                hist_action = "Manager Approved"
                hr_user = db.execute("SELECT id FROM users WHERE role='hr' AND is_active=1 ORDER BY id LIMIT 1").fetchone()
                if hr_user:
                    notify_user(hr_user["id"], "Leave request pending HR review", f"{leave['application_no']} is ready for HR review.", url_for("leave_detail", leave_id=leave_id))
                notify_user(leave["user_id"], "Manager approved leave", f"{leave['application_no']} has moved to HR review.", url_for("leave_detail", leave_id=leave_id))
            elif action == "reject":
                db.execute("UPDATE leave_applications SET manager_status='Rejected', status='Rejected by Manager', current_stage='closed' WHERE id=?", (leave_id,))
                hist_action = "Manager Rejected"
                notify_user(leave["user_id"], "Leave rejected", f"{leave['application_no']} was rejected by your manager.", url_for("leave_detail", leave_id=leave_id))
        elif user["role"] in {"hr", "admin"} and leave["manager_status"] == "Approved" and leave["hr_status"] == "Pending":
            if action == "approve":
                db.execute("UPDATE leave_applications SET hr_status='Approved', status='Final Approved', current_stage='closed' WHERE id=?", (leave_id,))
                db.execute("UPDATE leave_balances SET used_days=used_days+?, remaining_days=remaining_days-? WHERE user_id=? AND leave_type_id=?", (leave["total_days"], leave["total_days"], leave["user_id"], leave["leave_type_id"]))
                hist_action = "HR Approved"
                notify_user(leave["user_id"], "Leave approved", f"{leave['application_no']} was finally approved.", url_for("leave_detail", leave_id=leave_id))
            elif action == "reject":
                db.execute("UPDATE leave_applications SET hr_status='Rejected', status='Rejected by HR', current_stage='closed' WHERE id=?", (leave_id,))
                hist_action = "HR Rejected"
                notify_user(leave["user_id"], "Leave rejected", f"{leave['application_no']} was rejected by HR.", url_for("leave_detail", leave_id=leave_id))
        if hist_action:
            db.execute("INSERT INTO leave_history(leave_application_id, action, remarks, action_by, action_at) VALUES (?, ?, ?, ?, ?)", (leave_id, hist_action, remarks, user["id"], now_str()))
            log_audit("Leave", hist_action, f"Leave request {leave['application_no']} actioned", leave["user_id"])
            db.commit()
            flash("Action saved successfully.", "success")
            return redirect(url_for("leave_detail", leave_id=leave_id))
    history = db.execute("SELECT lh.*, u.full_name FROM leave_history lh LEFT JOIN users u ON lh.action_by=u.id WHERE lh.leave_application_id=? ORDER BY lh.id ASC", (leave_id,)).fetchall()
    return render_template("leave_detail.html", leave=leave, history=history)


@app.route("/team")
@login_required
@role_required("manager", "hr", "admin")
def team():
    user = current_user()
    db = get_db()
    search = request.args.get("q", "").strip()
    query = "SELECT u.*, d.name AS department_name, ds.name AS designation_name FROM users u LEFT JOIN departments d ON u.department_id=d.id LEFT JOIN designations ds ON u.designation_id=ds.id"
    conditions = []
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
    managers = db.execute("SELECT id, full_name FROM users WHERE role='manager' AND is_active=1 ORDER BY full_name").fetchall()
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
                "INSERT INTO users(full_name, email, employee_code, password_hash, role, department_id, designation_id, manager_id, phone, address, emergency_contact, join_date, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (form["full_name"].strip(), form["email"].strip(), form["employee_code"].strip(), generate_password_hash(form["password"]), form["role"], form["department_id"], form["designation_id"], manager_id, form["phone"].strip(), form["address"].strip(), form["emergency_contact"].strip(), form["join_date"], 1 if form.get("is_active", "1") == "1" else 0),
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
            "UPDATE users SET full_name=?, email=?, employee_code=?, role=?, department_id=?, designation_id=?, manager_id=?, phone=?, address=?, emergency_contact=?, join_date=?, is_active=? WHERE id=?",
            (form["full_name"].strip(), form["email"].strip(), form["employee_code"].strip(), form["role"], form["department_id"], form["designation_id"], manager_id, form["phone"].strip(), form["address"].strip(), form["emergency_contact"].strip(), form["join_date"], 1 if form.get("is_active", "1") == "1" else 0, user_id),
        )
        notify_user(user_id, "Profile updated", "Your employee profile details were updated by HR/Admin.", url_for("employee_detail", user_id=user_id))
        log_audit("Employee", "Updated", f"Updated employee {form['employee_code']}", user_id)
        db.commit()
        flash("Employee updated successfully.", "success")
        return redirect(url_for("employee_detail", user_id=user_id))
    return render_template("employee_form.html", departments=departments, designations=designations, managers=managers, employee=employee)


@app.route("/employees/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_employee(user_id: int):
    db = get_db()
    employee = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("team"))
    if employee["role"] == "admin":
        active_admins = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND is_active=1").fetchone()["c"]
        if active_admins <= 1:
            flash("You cannot delete the last active admin.", "danger")
            return redirect(url_for("employee_detail", user_id=user_id))
    db.execute("UPDATE users SET is_active=0, manager_id=NULL WHERE id=?", (user_id,))
    db.execute("UPDATE users SET manager_id=NULL WHERE manager_id=?", (user_id,))
    notify_user(user_id, "Account deactivated", "Your employee portal access has been deactivated.", None)
    log_audit("Employee", "Deactivated", f"Deactivated employee {employee['employee_code']}", user_id)
    db.commit()
    flash("Employee deactivated successfully.", "success")
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
    rows = db.execute("SELECT * FROM attendance WHERE user_id=? AND substr(attendance_date,1,7)=? ORDER BY attendance_date DESC", (selected_user_id, month)).fetchall()
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
    }
    selected_employee = db.execute("SELECT id, full_name FROM users WHERE id=?", (selected_user_id,)).fetchone()
    return render_template("attendance.html", rows=rows, employees=employees, selected_user_id=selected_user_id, month=month, summary=summary, selected_employee=selected_employee)


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
