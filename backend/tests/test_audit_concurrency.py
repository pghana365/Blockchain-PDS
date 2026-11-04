"""Audit hash-chain under concurrent writers.

Runs against a REAL MySQL server (the project's docker container), in a separate
database called `pds_test`, because the bug is about row locking and SQLite cannot
reproduce it. Skipped automatically when MySQL is not reachable.
"""
import os
import threading
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import create_app
from app.extensions import db
from app.models import AuditLog
from app.services import audit
from config import TestConfig

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _mysql_test_url():
    base = os.environ.get("DATABASE_URL", "")
    root_pw = os.environ.get("MYSQL_ROOT_PASSWORD", "")
    if not base.startswith("mysql") or not root_pw:
        return None
    url = make_url(base)
    try:
        admin = create_engine(url.set(username="root", password=root_pw, database=None), connect_args={"connect_timeout": 3})
        with admin.begin() as c:
            c.execute(text("CREATE DATABASE IF NOT EXISTS pds_test CHARACTER SET utf8mb4"))
            c.execute(text(f"GRANT ALL ON pds_test.* TO '{url.username}'@'%'"))
        admin.dispose()
    except Exception:
        return None
    return url.set(database="pds_test").render_as_string(hide_password=False)


MYSQL_URL = _mysql_test_url()
pytestmark = pytest.mark.skipif(MYSQL_URL is None, reason="MySQL test database not reachable")


@pytest.fixture()
def mysql_app():
    class MySqlConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = MYSQL_URL
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_size": 20, "pool_pre_ping": True}

    app = create_app(MySqlConfig)
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _writer(app, tag, n, errors):
    try:
        with app.app_context():
            for i in range(n):
                audit.log("CONCURRENCY_TEST", None, "test", f"{tag}-{i}", {"i": i}, commit=True, username=f"writer{tag}")
            db.session.remove()
    except Exception as exc:  # pragma: no cover - reported via assertion below
        errors.append(repr(exc))


def test_concurrent_audit_writes_keep_the_chain_valid(mysql_app):
    threads_n, per_thread = 12, 6
    errors = []
    threads = [threading.Thread(target=_writer, args=(mysql_app, t, per_thread, errors)) for t in range(threads_n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors

    with mysql_app.app_context():
        entries = AuditLog.query.order_by(AuditLog.id).all()
        assert len(entries) == threads_n * per_thread
        # every entry points at the hash of the entry right before it - no forks
        prevs = [e.prev_hash for e in entries]
        assert len(set(prevs)) == len(prevs), "two entries share the same previous hash (chain forked)"
        ok, checked, bad = audit.verify_chain()
        assert ok, f"chain broken at entry {bad} after {checked} verified"
        assert checked == threads_n * per_thread


def test_chain_continues_correctly_after_restart(mysql_app):
    """The chain head must be derived from existing entries, not start again from genesis."""
    with mysql_app.app_context():
        audit.log("FIRST", None, commit=True, username="a")
        audit.log("SECOND", None, commit=True, username="a")
        first = AuditLog.query.order_by(AuditLog.id).first()
        second = AuditLog.query.order_by(AuditLog.id).all()[1]
        assert second.prev_hash == first.entry_hash
        assert audit.verify_chain()[0]
