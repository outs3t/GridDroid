import urllib.request

HOST = "http://127.0.0.1:8470"
ORIGIN = "http://127.0.0.1:8470"
SERIAL = "RF8NA06233B"

def call(path, method="POST"):
    req = urllib.request.Request(f"{HOST}{path}", method=method, headers={"Origin": ORIGIN})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except Exception as e:
        return -1, str(e)

print("focus:", call(f"/api/input/focus/{SERIAL}"))
print("tap:", call(f"/api/input/tap?x=540&y=1200&w=1080&h=2400"))
