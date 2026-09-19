#!/usr/bin/env python3
"""Build the state data-center policy layer with an LLM research pass.

Design rules, in order of importance:

1. Every scored field must carry at least one source URL. Uncited fields are
   dropped, not guessed. An LLM asserting "Ohio has a sales tax exemption"
   with no link is not evidence.
2. Results are cached per state as JSON. Re-running costs nothing for states
   already done, so a failed run never re-bills the whole country.
3. Model output is data, never instructions. We parse it as JSON and read
   only the fields we asked for.

Usage:
    python pipeline/policy_scan.py            # all CONUS states (cached)
    python pipeline/policy_scan.py OH VA TX   # just these
    python pipeline/policy_scan.py --force OH # ignore cache
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "raw" / "policy"
MODEL = "claude-sonnet-5"

STATES = {
    "AL":"01","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10",
    "DC":"11","FL":"12","GA":"13","ID":"16","IL":"17","IN":"18","IA":"19",
    "KS":"20","KY":"21","LA":"22","ME":"23","MD":"24","MA":"25","MI":"26",
    "MN":"27","MS":"28","MO":"29","MT":"30","NE":"31","NV":"32","NH":"33",
    "NJ":"34","NM":"35","NY":"36","NC":"37","ND":"38","OH":"39","OK":"40",
    "OR":"41","PA":"42","RI":"44","SC":"45","SD":"46","TN":"47","TX":"48",
    "UT":"49","VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56",
}

FIELDS = [
    ("sales_tax_exemption",  "Sales/use tax exemption on data center equipment"),
    ("property_tax_abatement","Property tax abatement available for data centers"),
    ("permitting_speed",      "Speed/predictability of siting and permitting"),
    ("moratoria_active",      "Active local moratoria or restrictions on DC development"),
    ("power_ratepayer_rules", "Who pays interconnection//grid upgrade costs for large load"),
    ("water_restrictions",    "Restrictions on water use for cooling"),
]

PROMPT = """Research the current policy environment for large data center \
development in the US state of {name} ({code}).

For each field below, give a score from 0 to 100 where 100 is most favourable \
to a data center developer, plus at least one source URL published by a \
government body, utility regulator, or established news outlet.

Fields:
{fields}

Rules you must follow:
- If you cannot find a real source for a field, set its score to null and \
leave sources empty. Do not guess and do not infer from neighbouring states.
- Every URL must be one you actually retrieved in this search. Never \
construct a plausible-looking URL.
- moratoria_active: 100 means no known moratoria; 0 means widespread active \
moratoria.
- water_restrictions: 100 means no meaningful restriction on cooling water.

Reply with ONLY a JSON object, no prose:
{{"state":"{code}","fields":{{"<field>":{{"score":<0-100 or null>,\
"note":"<=25 words","sources":["<url>",...]}}, ...}},"as_of":"<YYYY-MM>"}}"""


def _env(name: str) -> str | None:
    envfile = ROOT / ".env"
    if envfile.exists() and name not in os.environ:
        for line in envfile.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get(name)


def ask(code: str, key: str) -> dict:
    name = code
    body = {
        "model": MODEL,
        "max_tokens": 3000,
        "tools": [{"type": "web_search_20250305", "name": "web_search",
                   "max_uses": 3}],
        "messages": [{"role": "user", "content": PROMPT.format(
            code=code, name=name,
            fields="\n".join(f"- {k}: {d}" for k, d in FIELDS))}],
    }
    # Server-side web search makes these calls slow; 300s was not enough and
    # two of the first three states timed out. Retry once before giving up.
    last = None
    for attempt in range(2):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json",
                     "User-Agent": "dc-siting/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("error", {}).get("message", "")
            except Exception:
                pass
            # A 400 is a permanent problem (bad request, or -- the common case
            # here -- an exhausted credit balance). Retrying just burns time.
            if e.code == 400:
                raise RuntimeError(f"HTTP 400: {detail}") from None
            last = RuntimeError(f"HTTP {e.code}: {detail or e.reason}")
            time.sleep(15 if e.code == 429 else 10)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10)
    else:
        raise RuntimeError(f"api call failed twice: {last}")

    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text")
    usage = resp.get("usage", {})
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model reply")
    raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Models occasionally emit an unescaped quote inside a note string.
        # Repair just that case rather than discarding a paid-for response.
        fixed = re.sub(r'(?<="note":\s")(.*?)(?="\s*,\s*"sources")',
                       lambda mm: mm.group(1).replace('"', "'"), raw, flags=re.S)
        data = json.loads(fixed)
    data["_usage"] = {k: usage.get(k) for k in
                      ("input_tokens", "output_tokens",
                       "server_tool_use")}
    return data


URL_RE = re.compile(r"^https?://[^\s]+\.[^\s]+$")


def validate(rec: dict) -> dict:
    """Drop any field that is not backed by a well-formed source URL."""
    out, dropped = {}, []
    for k, _ in FIELDS:
        f = (rec.get("fields") or {}).get(k) or {}
        score = f.get("score")
        srcs = [u for u in (f.get("sources") or []) if URL_RE.match(str(u))]
        if score is None or not srcs:
            dropped.append(k)
            continue
        try:
            score = max(0.0, min(100.0, float(score)))
        except (TypeError, ValueError):
            dropped.append(k)
            continue
        out[k] = {"score": score, "note": str(f.get("note", ""))[:160],
                  "sources": srcs[:4]}
    return {"fields": out, "dropped": dropped, "as_of": rec.get("as_of")}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    codes = [a.upper() for a in args] or list(STATES)

    key = _env("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY not set"); return 1
    CACHE.mkdir(parents=True, exist_ok=True)

    tot_in = tot_out = tot_search = 0
    done = skipped = failed = 0
    for i, code in enumerate(codes):
        dest = CACHE / f"{code}.json"
        if dest.exists() and not force:
            skipped += 1
            continue
        try:
            rec = ask(code, key)
        except Exception as e:  # noqa: BLE001
            print(f"  {code}: FAILED {type(e).__name__} {str(e)[:70]}")
            failed += 1
            time.sleep(3)
            continue
        u = rec.pop("_usage", {})
        tot_in += u.get("input_tokens") or 0
        tot_out += u.get("output_tokens") or 0
        tot_search += ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        v = validate(rec)
        v["state"] = code
        dest.write_text(json.dumps(v, indent=1))
        kept = len(v["fields"])
        print(f"  {code}: {kept}/{len(FIELDS)} fields cited"
              + (f"  dropped={v['dropped']}" if v["dropped"] else ""))
        done += 1
        time.sleep(2)     # be polite to the API

    # Sonnet pricing: $3 / MTok in, $15 / MTok out. Web search $10 / 1k searches.
    cost = tot_in / 1e6 * 3 + tot_out / 1e6 * 15 + tot_search / 1000 * 10
    print(f"\nfetched={done} cached={skipped} failed={failed}")
    print(f"tokens in={tot_in:,} out={tot_out:,} searches={tot_search}")
    print(f"estimated cost this run: ${cost:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
