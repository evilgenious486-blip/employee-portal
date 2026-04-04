
CREATE TABLE IF NOT EXISTS users (
id INTEGER PRIMARY KEY AUTOINCREMENT,
full_name TEXT,
email TEXT UNIQUE,
employee_code TEXT UNIQUE,
password_hash TEXT,
role TEXT
);
