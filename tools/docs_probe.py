"""What Kalshi's docs say about subaccounts and subaccount-restricted API keys (probe only)."""
import re

import requests
import yaml

S = requests.Session()
idx = S.get("https://docs.kalshi.com/llms.txt", timeout=30).text
pages = []
for line in idx.splitlines():
    if re.search(r"subaccount|api key|api-key|transfer|restricted", line, re.I):
        print("  ", line[:260])
        m = re.search(r"\((https://docs\.kalshi\.com/[^)]+\.md)\)", line)
        if m:
            pages.append(m.group(1))
for url in pages[:12]:
    t = S.get(url, timeout=30).text.split("## OpenAPI")[0]
    print("\n=====", url, "=====")
    print(t[:3500])
log = S.get("https://docs.kalshi.com/changelog.md", timeout=30).text
print("\n===== changelog on subaccounts =====")
for m in re.finditer(r"\*\*([A-Z][a-z]+ \d{1,2}, 20\d\d)\*\*(.*?)(?=\*\*[A-Z][a-z]+ \d{1,2}, 20\d\d\*\*|\Z)", log, re.S):
    body = " ".join(m.group(2).split())
    if re.search(r"subaccount", body, re.I):
        print(f"--- {m.group(1)}: {body[:1200]}")
raw = S.get("https://docs.kalshi.com/openapi.yaml", timeout=60).text
spec = yaml.safe_load(re.sub(r"[\x80-\x9f]", "", raw))
print("\n===== subaccount params on the endpoints kal uses =====")
for p in ("/portfolio/balance", "/portfolio/positions", "/portfolio/orders", "/portfolio/fills",
          "/portfolio/events/orders", "/portfolio/events/orders/{order_id}", "/portfolio/settlements",
          "/historical/orders", "/historical/fills"):
    for verb, op in (spec["paths"].get(p) or {}).items():
        if not isinstance(op, dict):
            continue
        for prm in op.get("parameters", []) or []:
            if "$ref" in prm:
                prm = spec["components"]["parameters"].get(prm["$ref"].split("/")[-1], {})
            if "subaccount" in str(prm.get("name")):
                print(f"{verb.upper()} {p} param {prm.get('name')}: {(prm.get('description') or '').strip()[:300]}")
        body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
        if body:
            if "$ref" in body:
                body = spec["components"]["schemas"].get(body["$ref"].split("/")[-1], {})
            sub = (body.get("properties") or {}).get("subaccount")
            if sub:
                print(f"{verb.upper()} {p} body subaccount: {str(sub)[:300]}")
