r"""Deploy della landing page (gh-pages) con l'ultimo build.

Aggiorna landing/version.json, landing/index.html, copia l'eseguibile Windows
e fa push sul branch `gh-pages`. Usare così:

    python deploy_landing.py              # deploya dist\GridDroid.exe
    python deploy_landing.py --build      # builda e deploya
    python deploy_landing.py --installer  # deploya dist\GridDroid_Setup.exe
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List


REPO_ROOT = Path(__file__).resolve().parent
LANDING_DIR = REPO_ROOT / "landing"
DIST_DIR = REPO_ROOT / "dist"


def get_version() -> str:
    init_file = REPO_ROOT / "griddroid" / "__init__.py"
    text = init_file.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not match:
        raise RuntimeError("Impossibile trovare __version__ in griddroid/__init__.py")
    return match.group(1)


def run(cmd: List[str], *, cwd: Path | None = None, check: bool = True) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=check)


def build_onefile() -> None:
    print("Build di GridDroid.exe con PyInstaller...")
    run([sys.executable, "-m", "PyInstaller", "griddroid.spec", "--clean", "--noconfirm"], cwd=REPO_ROOT)


def build_installer() -> None:
    print("Build dell'installer con Inno Setup...")
    run(["build_setup.bat"], cwd=REPO_ROOT)


def update_landing(exe_name: str, version: str) -> None:
    src_exe = DIST_DIR / exe_name
    if not src_exe.exists():
        raise FileNotFoundError(f"Manca {src_exe}. Build prima con --build o --installer.")

    dst_exe = LANDING_DIR / exe_name
    print(f"Copio eseguibile: {src_exe} -> {dst_exe}")
    shutil.copy2(src_exe, dst_exe)

    # Pulisco eseguibili vecchi
    for old in ["GridDroid_Setup.exe", "GridDroid.exe"]:
        if old != exe_name:
            old_path = LANDING_DIR / old
            if old_path.exists():
                print(f"Rimuovo eseguibile vecchio: {old_path}")
                old_path.unlink()

    # version.json
    version_file = LANDING_DIR / "version.json"
    build_time = datetime.now().astimezone().isoformat()
    data = {
        "version": version,
        "build_time": build_time,
        "windows": {
            "download_url": f"https://outs3t.github.io/GridDroid/{exe_name}",
            "silent_args": [] if "Setup" not in exe_name else ["/SILENT"],
        },
        "linux": {
            "download_url": "https://raw.githubusercontent.com/outs3t/GridDroid/main/install_linux.sh",
            "silent_args": [],
        },
    }
    version_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Aggiornato {version_file} alla versione {version} ({build_time})")

    # index.html — aggiorna riferimenti al nome eseguibile
    index_file = LANDING_DIR / "index.html"
    if index_file.exists():
        html = index_file.read_text(encoding="utf-8")
        for old in ["GridDroid_Setup.exe", "GridDroid.exe"]:
            html = html.replace(old, exe_name)
        # Testo esplicativo per l'eseguibile portatile
        if "Setup" not in exe_name:
            html = html.replace("Installer .exe. Non richiede Python, ADB o installazioni aggiuntive.",
                                "Versione portatile .exe. Non richiede installazione, basta avviarla.")
        index_file.write_text(html, encoding="utf-8")
        print(f"Aggiornato {index_file}")


def push_to_gh_pages(version: str) -> None:
    # Allinea il branch locale con il remoto per evitare conflitti
    run(["git", "fetch", "origin", "gh-pages"], cwd=REPO_ROOT)
    run(["git", "branch", "-f", "gh-pages", "origin/gh-pages"], cwd=REPO_ROOT)

    temp_dir = Path(tempfile.mkdtemp(prefix="griddroid_gh_pages_"))
    worktree = temp_dir / "gh_pages"
    try:
        print(f"Creo worktree gh-pages in {worktree}")
        run(["git", "worktree", "add", str(worktree), "gh-pages", "--force"], cwd=REPO_ROOT)

        # Pulisco il contenuto vecchio
        for item in worktree.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        # Copio i file aggiornati della landing
        for item in LANDING_DIR.iterdir():
            dst = worktree / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)

        # Commit e push
        run(["git", "add", "-A"], cwd=worktree)
        try:
            run(["git", "commit", "-m", f"Deploy versione {version}"], cwd=worktree)
        except subprocess.CalledProcessError:
            print("Nessun cambiamento da committare.")
            return
        run(["git", "push", "--force-with-lease", "origin", "gh-pages"], cwd=worktree)
        print("Landing pubblicata su gh-pages.")
    finally:
        try:
            run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy landing page")
    parser.add_argument("--build", action="store_true", help="Builda GridDroid.exe prima di deployare")
    parser.add_argument("--installer", action="store_true", help="Usa dist\\GridDroid_Setup.exe")
    parser.add_argument("--no-push", action="store_true", help="Aggiorna landing/ ma non pusha")
    args = parser.parse_args()

    version = get_version()
    print(f"Versione rilevata: {version}")

    if args.build:
        if args.installer:
            build_installer()
            exe_name = "GridDroid_Setup.exe"
        else:
            build_onefile()
            exe_name = "GridDroid.exe"
    else:
        exe_name = "GridDroid_Setup.exe" if args.installer else "GridDroid.exe"

    update_landing(exe_name, version)

    if not args.no_push:
        push_to_gh_pages(version)
        print("\nFatto. Il sito si aggiornera' tra qualche minuto.")
    else:
        print("\nLanding aggiornata in locale, push non richiesto.")


if __name__ == "__main__":
    main()
