"""Order direction fields and their deprecation, from Kalshi's docs (probe only)."""
import re

import requests
import yaml

S = requests.Session()
t = S.get("https://docs.kalshi.com/getting_started/order_direction.md", timeout=30).text
print(t.split("## OpenAPI")[0][:12000])
print("=" * 80)
log = S.get("https://docs.kalshi.com/changelog.md", timeout=30).text
for m in re.finditer(r"\*\*([A-Z][a-z]+ \d{1,2}, 20\d\d)\*\*(.*?)(?=\*\*[A-Z][a-z]+ \d{1,2}, 20\d\d\*\*|\Z)", log, re.S):
    body = m.group(2)
    if re.search(r"outcome_side|book_side|legacy|deprecat|remov|client_order_id|historical|/portfolio/fills|"
                 r"/portfolio/orders|fill", body, re.I):
        print(f"--- {m.group(1)}: {' '.join(body.split())[:900]}")
print("=" * 80)
raw = S.get("https://docs.kalshi.com/openapi.yaml", timeout=60).text
spec = yaml.safe_load(re.sub(r"[\x80-\x9f]", "", raw))
for name in ("Order", "Fill"):
    props = spec["components"]["schemas"][name]["properties"]
    for k in ("side", "action", "outcome_side", "book_side", "yes_price_dollars", "no_price_dollars", "count_fp",
              "fill_count_fp", "created_time", "last_update_time", "status", "client_order_id"):
        if k in props:
            v = props[k]
            print(f"{name}.{k}: deprecated={v.get('deprecated')} enum={v.get('enum')} "
                  f"ref={v.get('$ref')} desc={(v.get('description') or '').strip()[:300]!r}")
for k, v in spec["components"]["schemas"].items():
    if k in ("OutcomeSide", "BookSide", "OrderStatus"):
        print(k, v)
