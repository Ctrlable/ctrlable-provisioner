"""selfupdate.check_update — behind-detection against a remote ref.

Run:  python3 backend/tests/test_update_check.py

Fail-first is inherent: check_update did not exist before this change, so the
import/attribute access below was red until it landed. Builds a throwaway git
remote + clone locally (no network) and drives the real function.
"""
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import selfupdate  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                   cwd=cwd, check=True, capture_output=True)


tmp = tempfile.mkdtemp()
remote = pathlib.Path(tmp) / "remote.git"
work = pathlib.Path(tmp) / "work"
clone = pathlib.Path(tmp) / "clone"
subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
for n in ("c1", "c2"):
    (work / n).write_text("")
    git(work, "add", ".")
    git(work, "commit", "-m", n)
git(work, "push", "origin", "main")

subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
git(clone, "reset", "--hard", "HEAD~1")           # clone is now 1 commit behind
selfupdate.REPO_DIR = clone

r = selfupdate.check_update("main")
check("behind: update_available true + behind==1 when the clone is one back",
      r["update_available"] is True and r["behind"] == 1, str(r))
check("behind: latest != current while behind",
      r["latest"] != r["current"] and r["fetch_ok"] is True, str(r))

git(clone, "merge", "--ff-only", "origin/main")   # now current == latest
r2 = selfupdate.check_update("main")
check("current: update_available false + behind==0 once caught up",
      r2["update_available"] is False and r2["behind"] == 0, str(r2))

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all update-check tests passed")
