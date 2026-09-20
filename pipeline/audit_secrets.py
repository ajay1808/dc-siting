#!/usr/bin/env python3
"""Fail if any .env value reached git history or the published web/ directory.

Run before publishing, and any time a new key is added. On a public repo a key
committed once is exposed permanently, so this checks all refs, not just HEAD.
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    envfile = ROOT / ".env"
    if not envfile.exists():
        print("no .env - nothing to audit")
        return 0
    env = {k: v.strip() for k, v in
           (l.split("=", 1) for l in envfile.read_text().splitlines() if "=" in l)}

    bad = False
    if subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT,
                      capture_output=True).returncode == 0:
        print("FAIL .env is tracked by git")
        bad = True

    for name, val in env.items():
        if not val:
            continue
        found = subprocess.run(["git", "log", "-S", val, "--oneline", "--all"],
                               cwd=ROOT, capture_output=True, text=True).stdout.strip()
        print(f"  {name:<20}{'FOUND IN HISTORY' if found else 'clean'}")
        bad |= bool(found)

    pats = [r"sk-ant-[A-Za-z0-9_\-]{20,}", r"\b[A-Za-z0-9]{8}\.[A-Za-z0-9]{32}\b"]
    for p in (ROOT / "web").rglob("*"):
        if not p.is_file() or p.suffix == ".pmtiles":
            continue
        t = p.read_text(errors="ignore")
        for pat in pats:
            if re.search(pat, t):
                print(f"FAIL secret-shaped string in {p.relative_to(ROOT)}")
                bad = True

    print("AUDIT FAILED" if bad else "audit clean - safe to publish")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
