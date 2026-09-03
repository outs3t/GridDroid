"""Test del canale di controllo scrcpy: verifica che il server accetti le
opzioni, che entrambi i socket (video + controllo) si connettano e che
l'iniezione di eventi nativi funzioni.

Uso: python test_control.py
"""

import asyncio
import random
import subprocess
import sys
from pathlib import Path

from griddroid.control_channel import ControlChannel, ACTION_DOWN, ACTION_UP

SCRCPY_VERSION = "4.1"
JAR = "tools/scrcpy-server"
REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"
ADB = str(Path(__file__).parent / "tools" / "adb.exe")


def sh(*args):
    args = [ADB if a == "adb" else a for a in args]
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


async def main():
    devices = sh("adb", "devices").stdout
    serial = None
    for line in devices.splitlines()[1:]:
        if "\tdevice" in line:
            serial = line.split("\t")[0]
            break
    if not serial:
        print("Nessun dispositivo collegato")
        return 1
    print(f"Dispositivo: {serial}")

    port = 27183 + random.randint(0, 999)
    scid = f"{random.randint(0, 0x7FFFFFFF):08x}"

    print("Push server...")
    sh("adb", "-s", serial, "push", JAR, REMOTE_JAR)

    print(f"Forward tcp:{port} -> localabstract:scrcpy_{scid}")
    sh("adb", "-s", serial, "forward", f"tcp:{port}", f"localabstract:scrcpy_{scid}")

    cmd = (
        f"CLASSPATH={REMOTE_JAR} "
        f"app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
        f"tunnel_forward=true "
        f"audio=false control=true cleanup=false "
        f"send_device_meta=false send_frame_meta=false "
        f"send_dummy_byte=false "
        f"max_size=1080 max_fps=30 video_bit_rate=8000000 "
        f"scid={scid}"
    )
    print("Avvio server...")
    proc = subprocess.Popen(
        [ADB, "-s", serial, "shell", cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    try:
        await asyncio.sleep(1.5)

        print("Connessione socket video...")
        vreader, vwriter = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=5)
        print("  video OK")

        print("Connessione socket controllo...")
        _, cwriter = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=5)
        ctrl = ControlChannel(serial, cwriter)
        print("  controllo OK")

        # Legge un po' di video per confermare che il flusso parta
        data = await asyncio.wait_for(vreader.read(65536), timeout=5)
        head = data[:8].hex()
        print(f"Video: {len(data)} bytes ricevuti, inizio = {head}")
        if b"\x00\x00\x00\x01" not in data[:64] and b"\x00\x00\x01" not in data[:64]:
            print("  ATTENZIONE: nessuno start code H264 all'inizio")
        else:
            print("  start code H264 presente")

        # Test iniezione: tocco al centro dello schermo
        print("Iniezione touch al centro (down+up)...")
        t0 = asyncio.get_event_loop().time()
        ok1 = await ctrl.touch(ACTION_DOWN, 540, 1200, 1080, 2400)
        ok2 = await ctrl.touch(ACTION_UP, 540, 1200, 1080, 2400)
        dt = (asyncio.get_event_loop().time() - t0) * 1000
        print(f"  inviato: {ok1 and ok2}, latenza invio: {dt:.2f}ms")

        # Test tasto HOME
        print("Iniezione tasto HOME...")
        ok3 = await ctrl.key_press(3)
        print(f"  inviato: {ok3}")

        # Test testo con accenti italiani
        print("Iniezione testo 'perché è così'...")
        ok4 = await ctrl.text("perché è così")
        print(f"  inviato: {ok4}")

        await asyncio.sleep(0.5)

        # Verifica che i socket siano ancora vivi: se il server avesse
        # rifiutato un messaggio si sarebbe chiuso
        print(f"Canale ancora attivo: {ctrl.alive}")
        data2 = await asyncio.wait_for(vreader.read(65536), timeout=5)
        print(f"Video continua a scorrere: {len(data2)} bytes")

        ctrl.close()
        vwriter.close()
        print("\nTEST SUPERATO")
        return 0

    except Exception as exc:
        print(f"\nERRORE: {type(exc).__name__}: {exc}")
        return 1
    finally:
        proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        if out.strip():
            print("\n--- Output server ---")
            print(out.strip())
        sh("adb", "-s", serial, "forward", "--remove", f"tcp:{port}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
