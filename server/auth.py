# server/auth.py

from datetime import datetime, timedelta
from typing import Optional

import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr

# ---- CONFIG ----

SECRET_KEY = os.getenv("SARA_AUTH_SECRET", "CHANGE_ME_IN_PROD")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

DB_PATH = os.getenv("SARA_DB", "sara.db")

router = APIRouter(prefix="/api/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


# ---- DB HELPERS ----

def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_users_table():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()


# run once on import
init_users_table()


# ---- MODELS ----

class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class User(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime


# ---- SECURITY HELPERS ----

def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def get_user_by_email(email: str) -> Optional[User]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, email, created_at FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return User(
        id=row["id"],
        email=row["email"],
        created_at=datetime.fromisoformat(row["created_at"])
        if isinstance(row["created_at"], str)
        else row["created_at"],
    )


def get_user_row_by_email(email: str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_by_id(user_id: int) -> Optional[User]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, email, created_at FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return User(
        id=row["id"],
        email=row["email"],
        created_at=datetime.fromisoformat(row["created_at"])
        if isinstance(row["created_at"], str)
        else row["created_at"],
    )


# ---- DEPENDENCY: CURRENT USER ----

async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_id(int(user_id))
    if user is None:
        raise credentials_exception
    return user


# ---- ROUTES ----

@router.post("/signup", response_model=Token)
def signup(payload: UserCreate):
    # basic: 8+ char password
    if len(payload.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters."
        )

    existing = get_user_row_by_email(payload.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered.")

    ph = hash_password(payload.password)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (payload.email, ph),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()

    access_token = create_access_token(data={"sub": user_id})
    return Token(access_token=access_token)


@router.post("/login", response_model=Token)
def login(payload: UserLogin):
    row = get_user_row_by_email(payload.email)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    if not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    access_token = create_access_token(data={"sub": row["id"]})
    return Token(access_token=access_token)


@router.get("/me", response_model=User)
async def me(user: User = Depends(get_current_user)):
    return user