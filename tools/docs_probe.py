"""Kalshi's API key page in full: how Ed25519 requests are signed (probe only)."""
import requests

t = requests.get("https://docs.kalshi.com/getting_started/api_keys.md", timeout=30).text
i = t.find("## Using a API Key")
print(t[i:i + 9000])
