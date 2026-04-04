CREATE TABLE departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE designations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_code TEXT NOT NULL UNIQUE,
    project_name TEXT NOT NULL,
    location TEXT,
    client_name TEXT,
    status TEXT NOT NULL DEFAULT 'Active',
    start_date TEXT,
    end_date TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    employee_code TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    department_id INTEGER,
    designation_id INTEGER,
    manager_id INTEGER,
    project_id INTEGER,
    phone TEXT,
    address TEXT,
    emergency_contact TEXT,
    join_date TEXT,
    monthly_basic REAL NOT NULL DEFAULT 0,
    default_allowances REAL NOT NULL DEFAULT 0,
    deduction_per_absent REAL NOT NULL DEFAULT 0,
    deduction_per_late REAL NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    avatar_filename TEXT,
    FOREIGN KEY (department_id) REFERENCES departments (id),
    FOREIGN KEY (designation_id) REFERENCES designations (id),
    FOREIGN KEY (manager_id) REFERENCES users (id),
    FOREIGN KEY (project_id) REFERENCES projects (id)
);

CREATE TABLE leave_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    annual_quota INTEGER NOT NULL DEFAULT 0,
    is_paid INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE leave_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    leave_type_id INTEGER NOT NULL,
    total_days INTEGER NOT NULL DEFAULT 0,
    used_days INTEGER NOT NULL DEFAULT 0,
    remaining_days INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (leave_type_id) REFERENCES leave_types (id)
);

CREATE TABLE leave_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_no TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    leave_type_id INTEGER NOT NULL,
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    total_days INTEGER NOT NULL,
    reason TEXT,
    attachment TEXT,
    status TEXT NOT NULL,
    manager_status TEXT NOT NULL DEFAULT 'Pending',
    hr_status TEXT NOT NULL DEFAULT 'Pending',
    current_stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (leave_type_id) REFERENCES leave_types (id)
);

CREATE TABLE leave_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leave_application_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    remarks TEXT,
    action_by INTEGER,
    action_at TEXT NOT NULL,
    FOREIGN KEY (leave_application_id) REFERENCES leave_applications (id),
    FOREIGN KEY (action_by) REFERENCES users (id)
);

CREATE TABLE employee_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    file_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    attendance_date TEXT NOT NULL,
    check_in TEXT,
    check_out TEXT,
    status TEXT NOT NULL,
    hours_worked REAL NOT NULL DEFAULT 0,
    ot_hours REAL NOT NULL DEFAULT 0,
    remarks TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE holiday_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    holiday_date TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    holiday_type TEXT NOT NULL DEFAULT "Holiday",
    created_at TEXT NOT NULL
);

CREATE TABLE payroll_slips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    month_label TEXT NOT NULL,
    basic_salary REAL NOT NULL,
    allowances REAL NOT NULL DEFAULT 0,
    deductions REAL NOT NULL DEFAULT 0,
    net_salary REAL NOT NULL,
    generated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE company_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    leave_workflow TEXT NOT NULL,
    default_working_hours REAL NOT NULL,
    allow_document_upload INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    target_user_id INTEGER,
    module_name TEXT NOT NULL,
    action_name TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users (id),
    FOREIGN KEY (target_user_id) REFERENCES users (id)
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    link TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE email_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_user_id INTEGER,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Queued',
    created_at TEXT NOT NULL,
    FOREIGN KEY (to_user_id) REFERENCES users (id)
);
