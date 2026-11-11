"""Firstboot call-home URL resolution (self-heals across site moves).

Run:  python3 backend/tests/test_orchestrator_url.py

Pure-stdlib (app.lan has no FastAPI import), so it runs without the venv.
Fail-first: on the old code resolve_orchestrator_url does not exist -> ImportError.
"""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.lan import resolve_orchestrator_url, detect_own_ip  # noqa: E402

fail = []
def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond: fail.append(label)

# explicit .env setting always wins (operator override)
check("explicit setting wins over detection",
      resolve_orchestrator_url("http://pinned:8000", detect=lambda: "1.2.3.4") == "http://pinned:8000")
# blank -> auto-detect the real-LAN IP (self-heal)
check("blank -> auto-detected real-LAN url",
      resolve_orchestrator_url("", detect=lambda: "10.0.0.188") == "http://10.0.0.188:8000")
# blank + detection fails -> empty so the caller omits the key (host falls back to build.conf)
check("blank + detect fails -> empty string",
      resolve_orchestrator_url("", detect=lambda: None) == "")
# detect_own_ip returns None or a dotted-quad, never a crash
ip = detect_own_ip()
check("detect_own_ip returns None or a dotted IPv4",
      ip is None or (ip.count(".") == 3 and not ip.startswith("127.")))

print()
if fail:
    print(f"FAILED ({len(fail)}): " + ", ".join(fail)); sys.exit(1)
print("all orchestrator-url checks passed")
