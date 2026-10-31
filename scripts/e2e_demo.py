"""End-to-end demo of the full scenario against the RUNNING stack
(MySQL + Fabric network + gateway + Flask).

    backend\\.venv\\Scripts\\python scripts\\e2e_demo.py [--bag BAG-0001]

It drives the real HTTP API exactly like the browser does (CSRF tokens, sessions).
"""
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:5000")
PASSWORD = os.environ["SEED_PASSWORD"]
bag_arg = sys.argv[sys.argv.index("--bag") + 1] if "--bag" in sys.argv else None

ok_count = 0


def check(label, cond, extra=""):
    global ok_count
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        sys.exit(1)
    ok_count += 1


class Client:
    def __init__(self, username):
        self.s = requests.Session()
        page = self.s.get(f"{BASE}/login").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = self.s.post(f"{BASE}/login", data={"username": username, "password": PASSWORD, "csrf_token": token},
                        allow_redirects=False)
        assert r.status_code == 302, f"login failed for {username}: {r.status_code}"
        self.username = username

    def csrf(self):
        html = self.s.get(f"{BASE}/dashboard").text
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def call(self, method, path, **kw):
        headers = {"X-CSRFToken": self.csrf()} if method != "GET" else {}
        return self.s.request(method, f"{BASE}{path}", headers=headers, **kw)


print("\n== 1. Admin registers a bag")
admin = Client("admin")
locs = admin.s.get(f"{BASE}/admin/bags").text
mls_id = re.search(r'<option value="(\d+)">MLS-01', locs).group(1)
fps_id = re.search(r'<option value="(\d+)">FPS-01', locs).group(1)
r = admin.call("POST", "/api/admin/bags", json={"mls_id": mls_id, "fps_id": fps_id, "bag_id": bag_arg or ""})
check("admin registers a bag", r.status_code == 201, r.text[:120])
bag = r.json()["created"][0]["bag_id"]
print(f"     bag = {bag}, register tx = {r.json()['created'][0]['tx_id'][:20]}...")

print("\n== 2. QR code")
q = admin.s.get(f"{BASE}/api/bags/{bag}/qr.png")
check("QR PNG generated for the bag", q.status_code == 200 and q.content[:4] == b"\x89PNG")

print("\n== 3. MLS operator dispatches 50.5 kg")
mls = Client("mls01.operator")
anon = requests.get(f"{BASE}/b/{bag}", allow_redirects=False)
check("unauthenticated QR scan redirects to login", anon.status_code == 302 and "/login" in anon.headers["Location"])
r = mls.call("POST", f"/api/bags/{bag}/dispatch", json={"weight_kg": "50.5"})
check("dispatch accepted", r.status_code == 201, r.text[:150])
print(f"     dispatch tx = {r.json()['tx_id']}")
check("bag is IN_TRANSIT", r.json()["bag"]["status"] == "IN_TRANSIT")
r = mls.call("POST", f"/api/bags/{bag}/dispatch", json={"weight_kg": "50.5"})
check("duplicate dispatch rejected", r.status_code == 409)

print("\n== 4. FPS operator: blind view")
fps = Client("fps01.operator")
v = fps.call("GET", f"/api/bags/{bag}")
txt = v.text
check("FPS can open the same QR target", v.status_code == 200 and v.json()["can_receive"])
check("FPS API response contains no dispatch weight", "50.5" not in txt and "dispatch" not in v.json())
check("FPS page contains no dispatch weight", "50.5" not in fps.s.get(f"{BASE}/b/{bag}").text)
other = Client("fps02.operator")
check("other FPS cannot see/receive this bag", other.call("GET", f"/api/bags/{bag}").status_code == 404
      and other.call("POST", f"/api/bags/{bag}/receive", json={"weight_kg": "50"}).status_code == 403)

print("\n== 5. FPS operator receives 47.5 kg")
r = fps.call("POST", f"/api/bags/{bag}/receive", json={"weight_kg": "47.5"})
check("receipt accepted", r.status_code == 201, r.text[:150])
rec = r.json()["bag"]["reconciliation"]
check("difference is 3.000 kg", rec["difference_kg"] == "3.000", f"tolerance {rec['tolerance_kg']}")
check("bag is FLAGGED with 'further verification' message", rec["status"] == "FLAGGED" and "further verification" in rec["message"])
check("reconciliation confirmed on the blockchain", rec["chain_status"] == "CONFIRMED", rec["chain_tx_id"][:20])
r = fps.call("POST", f"/api/bags/{bag}/receive", json={"weight_kg": "47.5"})
check("duplicate receipt rejected", r.status_code == 409)

print("\n== 6. Both operational parties see the result")
check("MLS sees final result", mls.call("GET", f"/api/bags/{bag}").json()["reconciliation"]["status"] == "FLAGGED")
check("FPS sees final result", fps.call("GET", f"/api/bags/{bag}").json()["reconciliation"]["status"] == "FLAGGED")

print("\n== 7. Auditor inspects the complete history")
aud = Client("auditor1")
d = aud.call("GET", f"/api/bags/{bag}").json()
fns = [t["function"] for t in d["transactions"]]
check("history has register/dispatch/receive/reconcile", fns == ["RegisterBag", "DispatchBag", "ReceiveBag", "RecordReconciliation"], str(fns))
check("auditor sees both weights", d["dispatch"]["weight_kg"] == "50.500" and d["receipt"]["weight_kg"] == "47.500")
check("auditor cannot dispatch", aud.call("POST", f"/api/bags/{bag}/dispatch", json={"weight_kg": "1"}).status_code == 403)
v = aud.call("GET", f"/api/audit/bags/{bag}/verify")
check("integrity check against the ledger passes", v.status_code == 200 and v.json()["ok"], v.text[:300])
for c in v.json()["checks"]:
    print(f"       - {'ok ' if c['ok'] else 'BAD'} {c['check']}")
h = aud.call("GET", f"/api/audit/bags/{bag}/chain-history").json()
check("on-chain state history", [x["status"] for x in h] == ["REGISTERED", "IN_TRANSIT", "RECEIVED", "FLAGGED"],
      " -> ".join(x["status"] for x in h))
check("audit-log hash chain intact", aud.call("GET", "/api/audit/log-integrity").json()["ok"])

print(f"\nALL {ok_count} END-TO-END CHECKS PASSED  (bag {bag})")
