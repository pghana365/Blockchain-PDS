import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # sessions / cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=int(os.environ.get("SESSION_MINUTES", "30")))
    REMEMBER_COOKIE_DURATION = timedelta(0)
    WTF_CSRF_TIME_LIMIT = None  # token valid for the session lifetime

    # login throttling
    MAX_FAILED_LOGINS = 5
    LOCKOUT_MINUTES = 15

    # Fabric gateway
    GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:3001")
    GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
    GATEWAY_TIMEOUT = int(os.environ.get("GATEWAY_TIMEOUT", "90"))
    # unknown-outcome handling: how often/long to ask the ledger about a transaction whose commit status was not seen
    LEDGER_CONFIRM_POLLS = int(os.environ.get("LEDGER_CONFIRM_POLLS", "4"))
    LEDGER_CONFIRM_INTERVAL = float(os.environ.get("LEDGER_CONFIRM_INTERVAL", "2.0"))
    PENDING_ABANDON_MINUTES = int(os.environ.get("PENDING_ABANDON_MINUTES", "15"))  # earliest an admin may abandon an unconfirmed op

    # business
    DEFAULT_TOLERANCE_KG = os.environ.get("DEFAULT_TOLERANCE_KG", "0.2")
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")  # embedded in QR codes
    SEED_PASSWORD = os.environ.get("SEED_PASSWORD", "")

    TEMPLATE_DIR = str(ROOT / "frontend" / "templates")
    STATIC_DIR = str(ROOT / "frontend" / "static")

    @classmethod
    def validate(cls):
        if len(cls.SECRET_KEY) < 32:
            raise RuntimeError("SECRET_KEY missing/too short - run: python scripts/make_env.py")
        if not cls.SQLALCHEMY_DATABASE_URI:
            raise RuntimeError("DATABASE_URL missing - run: python scripts/make_env.py")


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "t" * 40
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    GATEWAY_API_KEY = "test-key-test-key-test"
    SEED_PASSWORD = "Test#Password2026"
    MAX_FAILED_LOGINS = 5
    LEDGER_CONFIRM_POLLS = 3
    LEDGER_CONFIRM_INTERVAL = 0      # no sleeping in unit tests
    PENDING_ABANDON_MINUTES = 15

    @classmethod
    def validate(cls):
        pass
