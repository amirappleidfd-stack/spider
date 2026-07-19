from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import bcrypt

from app.config import DB_PATH

Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            hashed = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode()
            admin = User(username="admin", password_hash=hashed, theme="blue")
            db.add(admin)
            db.commit()
    finally:
        db.close()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    theme = Column(String(16), default="blue")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def verify_password(self, password: str) -> bool:
        return bcrypt.checkpw(password.encode(), self.password_hash.encode())

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    traffic_limit_gb = Column(Float, default=0.0)
    traffic_used_gb = Column(Float, default=0.0)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    @property
    def traffic_remaining_gb(self) -> float:
        if self.traffic_limit_gb <= 0:
            return float("inf")
        return max(0, self.traffic_limit_gb - self.traffic_used_gb)

    @property
    def traffic_percent(self) -> float:
        if self.traffic_limit_gb <= 0:
            return 0.0
        return min(100, (self.traffic_used_gb / self.traffic_limit_gb) * 100)

    @property
    def expires_in_days(self) -> int | None:
        if self.expires_at is None:
            return None
        delta = self.expires_at - datetime.utcnow()
        return max(0, delta.days)

class SubscriptionConfig(Base):
    __tablename__ = "subscription_configs"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    protocol = Column(String(16), nullable=False)
    config_data = Column(Text, nullable=False)
    country = Column(String(64), nullable=True)
    country_flag = Column(String(8), nullable=True)
    server = Column(String(128), nullable=True)
    port = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Setting(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False)
    value = Column(Text, nullable=False)

    @classmethod
    def get(cls, db: Session, key: str, default: str = "") -> str:
        row = db.query(cls).filter(cls.key == key).first()
        return row.value if row else default

    @classmethod
    def set(cls, db: Session, key: str, value: str) -> None:
        row = db.query(cls).filter(cls.key == key).first()
        if row:
            row.value = value
        else:
            row = cls(key=key, value=value)
            db.add(row)
        db.commit()

class TrafficStats(Base):
    __tablename__ = "traffic_stats"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, index=True, nullable=False)
    upload_bytes = Column(Integer, default=0)
    download_bytes = Column(Integer, default=0)
    timestamp = Column(DateTime, default=datetime.utcnow)

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hash_: str) -> bool:
    return bcrypt.checkpw(password.encode(), hash_.encode())