"""Prints what Kalshi's public docs say about order ids and order history (probe only)."""
import json
import re

import requests
import yaml

BASE = "https://docs.kalshi.com"
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


def show(text, pat, ctx=300, most=12):
    n = 0
    for m in re.finditer(pat, text, re.I):
        a, b = max(0, m.start() - ctx), min(len(text), m.end() + ctx)
        print("   ...", text[a:b].replace("\n", " "), "...")
        n += 1
        if n >= most:
            break
    print(f"   [{n} shown for {pat!r}]")


idx = get(f"{BASE}/llms.txt")
print("\n== llms.txt lines about orders, fills, history, changelog ==")
for line in idx.splitlines():
    if re.search(r"order|fill|histor|changelog|client|rate limit|settlement", line, re.I):
        print("  ", line[:220])

spec = None
for path in ("/openapi.yaml", "/openapi.json", "/api-reference/openapi.yaml", "/api-reference/openapi.json"):
    t = get(BASE + path)
    if t:
        try:
            spec = yaml.safe_load(t)
            print(f"spec from {path}: {len(spec.get('paths', {}))} paths")
            break
        except Exception as e:
            print(f"   not yaml/json: {e}")

if spec:
    print("\n== paths with orders, fills or historical ==")
    for p, ops in spec.get("paths", {}).items():
        if re.search(r"order|fill|histor", p):
            for verb, op in ops.items():
                if isinstance(op, dict):
                    print(f"  {verb.upper()} {p}: {op.get('summary', '')} | {(op.get('description') or '')[:400]}")
                    for prm in op.get("parameters", []) or []:
                        if isinstance(prm, dict) and "$ref" not in prm:
                            print(f"      param {prm.get('name')}: {(prm.get('description') or '')[:200]}")
                        elif isinstance(prm, dict):
                            print(f"      param {prm['$ref']}")
    print("\n== every schema field named client_order_id ==")
    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "client_order_id":
                    print(f"  {where}: {json.dumps(v)[:500]}")
                walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]")
    walk(spec, "spec")
    comps = spec.get("components", {})
    for name in ("parameters",):
        for k, v in (comps.get(name) or {}).items():
            if re.search(r"ts|cursor|status|limit", k, re.I):
                print(f"  components.{name}.{k}: {json.dumps(v)[:300]}")
    for k, v in (comps.get("schemas") or {}).items():
        if re.search(r"^Order$|CreateOrder|Fill$|Historical", k):
            print(f"\n  schema {k}: {json.dumps(v)[:1500]}")

for page in ("/api-reference/orders/get-orders", "/api-reference/portfolio/get-orders",
             "/getting_started/historical_data", "/changelog", "/getting_started/rate_limits"):
    t = get(BASE + page + ".md") or get(BASE + page)
    if t:
        show(t, r"histor|cutoff|older than|client_order_id|retention|archiv", ctx=250, most=8)
