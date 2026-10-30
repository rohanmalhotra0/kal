"""Every field Kalshi's spec marks deprecated, and breaking changes in its changelog (probe only)."""
import re

import requests
import yaml

S = requests.Session()
raw = S.get("https://docs.kalshi.com/openapi.yaml", timeout=60).text
spec = yaml.safe_load(re.sub(r"[\x80-\x9f]", "", raw))
for name, s in sorted(spec["components"]["schemas"].items()):
    for k, v in (s.get("properties") or {}).items() if isinstance(s, dict) else []:
        if isinstance(v, dict) and v.get("deprecated"):
            print(f"DEPRECATED {name}.{k}: {(v.get('description') or '').strip()[:220]!r}")
for p, ops in spec["paths"].items():
    for verb, op in ops.items():
        if isinstance(op, dict):
            if op.get("deprecated"):
                print(f"DEPRECATED endpoint {verb.upper()} {p}: {op.get('summary')}")
            for prm in op.get("parameters", []) or []:
                if isinstance(prm, dict) and prm.get("deprecated"):
                    print(f"DEPRECATED param {verb.upper()} {p} {prm.get('name')}")
log = S.get("https://docs.kalshi.com/changelog.md", timeout=30).text
print("=" * 60)
for m in re.finditer(r"\*\*([A-Z][a-z]+ \d{1,2}, 20\d\d)\*\*(.*?)(?=\*\*[A-Z][a-z]+ \d{1,2}, 20\d\d\*\*|\Z)", log, re.S):
    body = " ".join(m.group(2).split())
    if re.search(r"breaking|removed|will be removed|deprecat|no longer", body, re.I):
        print(f"--- {m.group(1)}: {body[:700]}")
