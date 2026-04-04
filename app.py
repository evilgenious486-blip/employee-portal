
from flask import Flask
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"

app = Flask(__name__)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    schema = BASE_DIR / "schema.sql"
    if not schema.exists():
        raise RuntimeError("schema.sql file missing")

    with get_db() as db:
        db.executescript(schema.read_text())

def seed_data():
    db = get_db()

    users = [
        ("Admin User","admin@example.com","EMP001","hash","Admin"),
        ("HR Manager","hr@example.com","EMP002","hash","HR"),
        ("Site Manager","manager@example.com","EMP003","hash","Manager"),
        ("Faisal Malik","faisal.malik@example.com","EMP004","hash","Engineer"),
    ]

    for u in users:
        try:
            db.execute(
                "INSERT INTO users (full_name,email,employee_code,password_hash,role) VALUES (?,?,?,?,?)",
                u,
            )
        except:
            pass

    db.commit()

@app.route("/")
def home():
    return "App running successfully"

if __name__ == "__main__":
    init_db()
    seed_data()
    app.run()
