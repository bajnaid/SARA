# server/seed_user.py

from server.auth import (
    init_users_table,
    get_db_conn,
    get_user_row_by_email,
    hash_password,
    DB_PATH,
)

# EDIT THESE IF YOU WANT DIFFERENT CREDENTIALS
DEFAULT_EMAIL = "saif@example.com"
DEFAULT_PASSWORD = "testpass123"


def create_user(email: str, password: str):
    # Make sure table exists
    init_users_table()

    existing = get_user_row_by_email(email)
    if existing:
        print(f"[seed_user] User already exists: {email}")
        return

    ph = hash_password(password)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email, ph),
    )
    conn.commit()
    conn.close()

    print(f"[seed_user] Created user {email} in DB at {DB_PATH}")


if __name__ == "__main__":
    create_user(DEFAULT_EMAIL, DEFAULT_PASSWORD)