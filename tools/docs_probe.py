"""Direction fields on real public trades, and the spec's Trade and position shapes (probe only)."""
import json
import re

import requests
import yaml

S = requests.Session()
API = "https://api.elections.kalshi.com/trade-api/v2"
d = S.get(API + "/markets/trades", params={"limit": 3}, timeout=30).json()
for t in d.get("trades", [])[:3]:
    print("trade:", json.dumps(t)[:600])
raw = S.get("https://docs.kalshi.com/openapi.yaml", timeout=60).text
spec = yaml.safe_load(re.sub(r"[\x80-\x9f]", "", raw))
sch = spec["components"]["schemas"]
for name in ("Trade", "MarketPosition"):
    s = sch.get(name, {})
    print(f"\n{name} required: {s.get('required')}")
    for k, v in (s.get("properties") or {}).items():
        print(f"  {name}.{k}: deprecated={v.get('deprecated')} {(v.get('description') or '').strip()[:160]!r}")
for name in ("Order", "Fill"):
    s = sch[name]
    print(f"\n{name} required: {s.get('required')}")
    for k in ("side", "action", "outcome_side", "book_side"):
        v = s["properties"].get(k, {})
        print(f"  {name}.{k}: deprecated={v.get('deprecated')} {json.dumps(v)[:300]}")
