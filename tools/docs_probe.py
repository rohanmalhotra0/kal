"""Page-size limits and other parameter rules on the account endpoints kal reads (probe only)."""
import json
import re

import requests
import yaml

raw = requests.get("https://docs.kalshi.com/openapi.yaml", timeout=60).text
spec = yaml.safe_load(re.sub(r"[\x80-\x9f]", "", raw))
comp = spec["components"]["parameters"]
for p in ("/portfolio/fills", "/historical/fills", "/portfolio/orders", "/historical/orders",
          "/portfolio/positions", "/portfolio/settlements", "/historical/markets", "/markets",
          "/markets/trades", "/historical/cutoff", "/historical/markets/{ticker}"):
    op = (spec["paths"].get(p) or {}).get("get")
    if not op:
        print(p, "missing")
        continue
    for prm in op.get("parameters", []) or []:
        if "$ref" in prm:
            prm = comp.get(prm["$ref"].split("/")[-1], {})
        if prm.get("name") in ("limit", "min_ts", "max_ts", "ticker", "subaccount", "cursor"):
            print(f"{p} {prm.get('name')}: schema={json.dumps(prm.get('schema'))} required={prm.get('required')}")
    print(f"{p} security: {op.get('security')}")
