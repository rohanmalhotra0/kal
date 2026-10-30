"""Prints the reply shapes of Kalshi's live and historical account endpoints (probe only)."""
import json
import re

import requests
import yaml

S = requests.Session()
S.headers["User-Agent"] = "kal-docs-probe"
raw = S.get("https://docs.kalshi.com/openapi.yaml", timeout=60).text
spec = yaml.safe_load(re.sub(r"[\x80-\x9f]", "", raw))
comp = spec.get("components", {})


def resolve(node, depth=0):
    if depth > 6:
        return "..."
    if isinstance(node, dict):
        if "$ref" in node:
            parts = node["$ref"].lstrip("#/").split("/")
            target = spec
            for p in parts:
                target = target.get(p, {})
            return resolve(target, depth + 1)
        return {k: resolve(v, depth + 1) for k, v in node.items() if k not in ("example", "examples")}
    if isinstance(node, list):
        return [resolve(v, depth + 1) for v in node]
    return node


def fields(schema):
    """Top-level fields of an object schema, with their types."""
    s = resolve(schema)
    out = {}
    for k, v in (s.get("properties") or {}).items():
        if isinstance(v, dict):
            out[k] = v.get("type") or ("enum" if "enum" in v else "?")
            if v.get("type") == "array":
                out[k] = f"array of {list((v.get('items') or {}).get('properties', {}).keys())[:60]}"
    return out, s.get("required")


for path in ("/portfolio/orders", "/historical/orders", "/portfolio/fills", "/historical/fills",
             "/historical/markets/{ticker}", "/markets/{ticker}", "/historical/cutoff", "/portfolio/positions"):
    op = spec["paths"].get(path, {}).get("get")
    if not op:
        print(path, "not in spec")
        continue
    ok = (op.get("responses") or {}).get("200", {})
    sch = ((resolve(ok).get("content") or {}).get("application/json") or {}).get("schema") or {}
    f, req = fields(sch)
    print(f"\n== GET {path} reply: {json.dumps(f)[:3000]}\n   required: {req}")

for name in ("Order", "Fill", "HistoricalOrder", "HistoricalFill"):
    if name in comp.get("schemas", {}):
        f, req = fields(comp["schemas"][name])
        print(f"\n== schema {name}: {json.dumps(f)[:3000]}\n   required: {req}")
