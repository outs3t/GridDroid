import urllib.request

serial = "RF8NA06233B"
req = urllib.request.Request(
    f"http://127.0.0.1:8470/api/stream/{serial}/start",
    method="POST",
    headers={"Origin": "http://127.0.0.1:8470"}
)
try:
    with urllib.request.urlopen(req) as resp:
        print(resp.read().decode())
except Exception as e:
    print("errore:", e)
