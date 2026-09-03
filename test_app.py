"""Test rapido per verificare che GridDroid funzioni correttamente."""

import asyncio
import json
import sys

# Colori per output leggibile
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
CHECK = f"{GREEN}✓{RESET}"
CROSS = f"{RED}✗{RESET}"
WARN = f"{YELLOW}⚠{RESET}"

BASE = "http://127.0.0.1:8470"
passed = 0
failed = 0


async def test_http(path: str, method: str = "GET", expected_status: int = 200, label: str = ""):
    """Testa un endpoint HTTP."""
    global passed, failed
    import urllib.request
    import urllib.error

    url = BASE + path
    name = label or f"{method} {path}"
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            if status == expected_status:
                print(f"  {CHECK} {name} → {status}")
                passed += 1
                try:
                    return json.loads(body) if body else None
                except json.JSONDecodeError:
                    return None
            else:
                print(f"  {CROSS} {name} → {status} (atteso {expected_status})")
                failed += 1
                return None
    except urllib.error.HTTPError as e:
        if e.code == expected_status:
            print(f"  {CHECK} {name} → {e.code}")
            passed += 1
        else:
            print(f"  {CROSS} {name} → HTTP {e.code} (atteso {expected_status})")
            failed += 1
        return None
    except Exception as e:
        print(f"  {CROSS} {name} → Errore: {e}")
        failed += 1
        return None


async def test_websocket():
    """Testa la connessione WebSocket."""
    global passed, failed
    try:
        import websockets
        async with websockets.connect("ws://127.0.0.1:8470/ws") as ws:
            # Dovrebbe ricevere un messaggio "devices" entro 2 secondi
            msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
            data = json.loads(msg)
            if data.get("type") == "devices":
                n = len(data.get("data", []))
                print(f"  {CHECK} WebSocket connesso — {n} dispositivi rilevati")
                passed += 1

                # Testa invio comando
                await ws.send(json.dumps({"action": "broadcast", "enabled": True}))
                # Aspetta aggiornamento
                msg2 = await asyncio.wait_for(ws.recv(), timeout=3.0)
                data2 = json.loads(msg2)
                if data2.get("type") in ("devices", "log"):
                    print(f"  {CHECK} WebSocket ricezione messaggi OK")
                    passed += 1
                else:
                    print(f"  {CROSS} WebSocket messaggio inatteso: {data2.get('type')}")
                    failed += 1

                # Riporta broadcast a false
                await ws.send(json.dumps({"action": "broadcast", "enabled": False}))
                return data.get("data", [])
            else:
                print(f"  {CROSS} WebSocket — messaggio inatteso: {data.get('type')}")
                failed += 1
                return []
    except ImportError:
        print(f"  {WARN} WebSocket — libreria 'websockets' non disponibile, skip")
        return []
    except Exception as e:
        print(f"  {CROSS} WebSocket — Errore: {e}")
        failed += 1
        return []


async def main():
    global passed, failed

    print(f"\n{'='*50}")
    print(f"  GridDroid — Test di funzionamento")
    print(f"{'='*50}\n")

    # 1. Pagina principale
    print("1. Frontend")
    await test_http("/", label="Pagina HTML principale")

    # 2. API REST
    print("\n2. API REST")
    devices = await test_http("/api/devices", label="Lista dispositivi")
    await test_http("/api/logs", label="Log di sistema")
    await test_http("/api/settings", label="Impostazioni")

    # 3. Bulk endpoints (senza file, 422 atteso)
    print("\n3. Bulk Actions (validazione)")
    await test_http("/api/bulk/wake-all", method="POST", label="Wake All")
    await test_http("/api/bulk/sleep-all", method="POST", label="Sleep All")
    await test_http("/api/bulk/shell?command=echo+test", method="POST", label="Shell broadcast")

    # 4. Input endpoints
    print("\n4. Input Relay")
    await test_http("/api/input/broadcast?enabled=true", method="POST", label="Broadcast ON")
    await test_http("/api/input/broadcast?enabled=false", method="POST", label="Broadcast OFF")

    # 5. WebSocket
    print("\n5. WebSocket")
    ws_devices = await test_websocket()

    # 6. Riepilogo dispositivi
    print(f"\n6. Dispositivi rilevati")
    if devices and len(devices) > 0:
        for d in devices:
            status_icon = {"online": GREEN+"●", "offline": RED+"●", "unauthorized": YELLOW+"●"}.get(d["status"], "●")
            print(f"  {status_icon}{RESET} {d['display_name']} ({d['serial']}) — {d['status']}")
    else:
        print(f"  {WARN} Nessun dispositivo collegato (normale se non hai telefoni USB)")

    # Risultato finale
    print(f"\n{'='*50}")
    total = passed + failed
    if failed == 0:
        print(f"  {CHECK} TUTTI I TEST PASSATI ({passed}/{total})")
    else:
        print(f"  {CROSS} {failed} test falliti su {total}")
    print(f"{'='*50}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)
