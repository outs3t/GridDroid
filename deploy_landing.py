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


def build_installer(version: str) -> None:
    """Compila l'installer Inno passando la versione corrente via /D."""
    print("Build dell'installer con Inno Setup...")
    iscc_candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
    ]
    iscc = next((p for p in iscc_candidates if os.path.exists(p)), None)
    if not iscc:
        raise RuntimeError(
            "Inno Setup non trovato. Installalo con:\n"
            "  winget install --id JRSoftware.InnoSetup -e --silent"
        )
    run([iscc, f"/DMyAppVersion={version}", "setup.iss"], cwd=REPO_ROOT)


def update_landing(version: str) -> None:
    """Copia installer + portable nella landing e aggiorna version.json."""
    base = "https://outs3t.github.io/GridDroid"
    for name in ("GridDroid_Setup.exe", "GridDroid.exe"):
        src = DIST_DIR / name
        if not src.exists():
            raise FileNotFoundError(f"Manca {src}. Build prima con --build.")
        dst = LANDING_DIR / name
        print(f"Copio: {src} -> {dst}")
        shutil.copy2(src, dst)

    # version.json: entrambi i canali; l'app sceglie in base a come gira
    version_file = LANDING_DIR / "version.json"
    build_time = datetime.now().astimezone().isoformat()
    data = {
        "version": version,
        "build_time": build_time,
        "windows": {
            "download_url": f"{base}/GridDroid_Setup.exe",
            "silent_args": ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
            "installer_url": f"{base}/GridDroid_Setup.exe",
            "installer_silent_args": ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
            "portable_url": f"{base}/GridDroid.exe",
        },
        "linux": {
            "download_url": "https://raw.githubusercontent.com/outs3t/GridDroid/main/install_linux.sh",
            "silent_args": [],
        },
    }
    version_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Aggiornato {version_file} alla versione {version} ({build_time})")


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
    parser.add_argument("--build", action="store_true", help="Builda exe + installer prima di deployare")
    parser.add_argument("--no-push", action="store_true", help="Aggiorna landing/ ma non pusha")
    args = parser.parse_args()

    version = get_version()
    print(f"Versione rilevata: {version}")

    if args.build:
        build_onefile()
        build_installer(version)

    update_landing(version)

    if not args.no_push:
        push_to_gh_pages(version)
        print("\nFatto. Il sito si aggiornera' tra qualche minuto.")
    else:
        print("\nLanding aggiornata in locale, push non richiesto.")


if __name__ == "__main__":
    main()
