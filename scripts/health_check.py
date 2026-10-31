"""Non-destructive check that every layer of the stack is up and wired together.

    backend\\.venv\\Scripts\\python scripts\\health_check.py
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
GW = os.environ.get("GATEWAY_URL", "http://127.0.0.1:3001")
KEY = os.environ["GATEWAY_API_KEY"]
APP = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:5000")
bad = 0


def line(name, ok, detail=""):
    global bad
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" - {detail}" if detail else ""))


try:
    r = requests.get(f"{APP}/login", timeout=5)
    line("Flask web app", r.status_code == 200, f"{APP}")
except requests.RequestException as e:
    line("Flask web app", False, str(e))

try:
    h = requests.get(f"{GW}/health", timeout=5).json()
    line("Fabric gateway", h.get("ok"), f"channel={h.get('channel')} chaincode={h.get('chaincode')}")
    for ident in ("pdsadmin", "mls01op", "fps01op", "auditor1"):
        i = requests.get(f"{GW}/identity/{ident}", headers={"x-api-key": KEY}, timeout=5).json()
        line(f"wallet identity {ident}", i.get("exists"), f"{i.get('mspId')} {i.get('role')} {i.get('location') or ''}")
    r = requests.post(f"{GW}/evaluate", headers={"x-api-key": KEY}, timeout=30,
                      json={"identity": "auditor1", "function": "GetBagStatus", "args": ["BAG-9999"]})
    line("peer + chaincode reachable (expects BAG_NOT_FOUND)", "BAG_NOT_FOUND" in r.text, r.text[:90])
except requests.RequestException as e:
    line("Fabric gateway", False, str(e))

sys.exit(1 if bad else 0)
