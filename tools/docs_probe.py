"""Prints what Kalshi's docs and API say about the historical tier (probe only)."""
import json
import re

import requests
import yaml

BASE = "https://docs.kalshi.com"
API = "https://api.elections.kalshi.com/trade-api/v2"
S = requests.Session()
S.headers["User-Agent"] = "kal-docs-probe"


def get(url):
    try:
        r = S.get(url, timeout=30)
        print(f"GET {url} -> {r.status_code} ({len(r.text)} chars)")
        return r.text if r.ok else ""
    except Exception as e:
        print(f"GET {url} -> {e}")
        return ""


def page(path, keep=None, limit=9000):
    """A docs page as markdown, without its OpenAPI block unless keep matches a line."""
    t = get(f"{BASE}{path}.md")
    body = t.split("## OpenAPI")[0]
    print(body[:limit])
    if keep:
        for line in t.splitlines():
            if re.search(keep, line, re.I):
                print("   >>", line.strip()[:400])
    print("-" * 80)


print("\n===== live API: historical cutoff (public?) =====")
for path in ("/historical/cutoff",):
    try:
        r = S.get(API + path, timeout=30)
        print(path, r.status_code, r.text[:1500])
    except Exception as e:
        print(path, e)

for p in ("/getting_started/historical_data",
          "/api-reference/historical/get-historical-cutoff-timestamps",
          "/api-reference/historical/get-historical-orders",
          "/api-reference/historical/get-historical-fills",
          "/api-reference/historical/get-historical-positions",
          "/api-reference/portfolio/get-fills",
          "/api-reference/portfolio/get-settlements"):
    print(f"\n===== {p} =====")
    page(p, keep=r"cutoff|archiv|histor|min_ts|max_ts|cursor|client_order_id|subaccount")

print("\n===== create order v2: client_order_id lines =====")
t = get(f"{BASE}/api-reference/orders/create-order-v2.md")
for m in re.finditer(r"client_order_id", t):
    print("   ...", t[max(0, m.start() - 200): m.end() + 400].replace("\n", " "))

print("\n===== openapi.yaml, cleaned =====")
raw = get(f"{BASE}/openapi.yaml")
clean = re.sub(r"[\x80-\x9f]", "", raw)
try:
    spec = yaml.safe_load(clean)
except Exception as e:
    print("still unreadable:", e)
    spec = {}
paths = spec.get("paths", {})
print(len(paths), "paths")
for p, ops in paths.items():
    if re.search(r"histor|/portfolio/orders|/portfolio/fills|/portfolio/events/orders$|/markets$", p):
        for verb, op in ops.items():
            if not isinstance(op, dict):
                continue
            print(f"\n  {verb.upper()} {p}: {op.get('summary', '')}")
            print("    ", (op.get("description") or "").replace("\n", " ")[:700])
            for prm in op.get("parameters", []) or []:
                if "$ref" in prm:
                    ref = prm["$ref"].split("/")[-1]
                    prm = spec.get("components", {}).get("parameters", {}).get(ref, {"name": ref})
                print(f"      param {prm.get('name')} ({prm.get('in')}): "
                      f"{(prm.get('description') or '').replace(chr(10), ' ')[:250]}")
schemas = spec.get("components", {}).get("schemas", {})
for name, s in schemas.items():
    props = s.get("properties", {}) if isinstance(s, dict) else {}
    if "client_order_id" in props:
        print(f"\n  schema {name}.client_order_id: {json.dumps(props['client_order_id'])[:400]}")
for name in schemas:
    if re.search(r"Cutoff|Historical", name):
        print(f"\n  schema {name}: {json.dumps(schemas[name])[:1200]}")
