import secrets, hashlib, json, hmac
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, Response
from sqlalchemy.orm import Session
from app.config import SECRET_KEY, SESSION_COOKIE_NAME, SESSION_MAX_AGE
from app.models import User, hash_password, verify_password

def _sign_data(data: str) -> str:
    h = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256)
    return h.hexdigest()

def _encode_session(username: str) -> str:
    expires = (datetime.utcnow() + timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    payload = json.dumps({"u": username, "e": expires})
    sig = _sign_data(payload)
    return f"{payload}.{sig}"

def _decode_session(cookie_value: str) -> Optional[str]:
    try:
        last_dot = cookie_value.rfind(".")
        if last_dot == -1:
            return None
        payload = cookie_value[:last_dot]
        sig = cookie_value[last_dot + 1:]
        expected = _sign_data(payload)
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        expires = datetime.fromisoformat(data["e"])
        if datetime.utcnow() > expires:
            return None
        return data["u"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

def set_session(response: Response, username: str) -> None:
    encoded = _encode_session(username)
    response.set_cookie(key=SESSION_COOKIE_NAME, value=encoded, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=False)

def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)

def get_current_user(request: Request) -> Optional[str]:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    return _decode_session(cookie)

def authenticate_user(db: Session, username: str, password: str) -> bool:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return False
    return user.verify_password(password)

def get_user(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()

def change_credentials(db: Session, username: str, new_username: Optional[str], new_password: Optional[str]) -> str:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return "User not found"
    if new_username and new_username != username:
        existing = db.query(User).filter(User.username == new_username).first()
        if existing:
            return "Username already taken"
        user.username = new_username
    if new_password:
        user.password_hash = hash_password(new_password)
    db.commit()
    return "Settings updated successfully"

def change_theme(db: Session, username: str, theme: str) -> str:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return "User not found"
    user.theme = theme
    db.commit()
    return "Theme updated"

def get_user_theme(db: Session, username: str) -> str:
    user = db.query(User).filter(User.username == username).first()
    return user.theme if user else "blue"