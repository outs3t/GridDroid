import json
import urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8470/api/logs?limit=200",
    headers={"Origin": "http://127.0.0.1:8470"}
)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    for e in data:
        print(f"{e['ts']:.2f} [{e.get('serial','-')}] {e['level']}: {e['message']}")
