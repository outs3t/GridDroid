#!/bin/bash
# Installer per GridDroid su Arch/Debian e derivati.
# Uso: ./install_linux.sh
# Copia l'applicazione in ~/.local/share/griddroid e crea il comando griddroid.

set -e

INSTALL_DIR="$HOME/.local/share/griddroid"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"

echo "=== GridDroid installer per Linux ==="
echo

# --- sudo helper -----------------------------------------------------------
if [ "$EUID" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

# --- rileva distro ---------------------------------------------------------
if [ -f /etc/os-release ]; then
  # shellcheck source=/dev/null
  . /etc/os-release
fi

ID_LOWER="${ID:-unknown}"
ID_LIKE_LOWER="${ID_LIKE:-}"

is_debian() { [ "$ID_LOWER" = "debian" ] || [ "$ID_LOWER" = "ubuntu" ] || [[ "$ID_LIKE_LOWER" == *"debian"* ]] || [[ "$ID_LIKE_LOWER" == *"ubuntu"* ]]; }
is_arch()   { [ "$ID_LOWER" = "arch" ] || [ "$ID_LOWER" = "manjaro" ] || [[ "$ID_LIKE_LOWER" == *"arch"* ]]; }

# --- dipendenze di sistema --------------------------------------------------
echo "Controllo dipendenze di sistema..."

install_debian() {
  echo "Rilevata derivata Debian/Ubuntu. Installo pacchetti..."
  $SUDO apt-get update
  $SUDO apt-get install -y \
    python3 python3-venv python3-pip python3-dev \
    adb android-tools-adb android-tools-fastboot \
    libjpeg-dev zlib1g-dev pkg-config || true
  # Dipendenze opzionali per la finestra nativa (pywebview/GTK)
  $SUDO apt-get install -y \
    python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
    libgirepository1.0-dev libcairo2-dev || true
}

install_arch() {
  echo "Rilevata derivata Arch/Manjaro. Installo pacchetti..."
  $SUDO pacman -Syu --noconfirm
  $SUDO pacman -S --needed --noconfirm \
    python python-pip python-virtualenv python-pygobject \
    android-tools libjpeg-turbo zlib pkgconf gcc || true
  # Dipendenze opzionali per la finestra nativa (pywebview/GTK)
  $SUDO pacman -S --needed --noconfirm \
    gtk3 webkit2gtk-4.1 gobject-introspection || true
}

if [ -z "$UPDATE_MODE" ]; then
  if is_debian; then
    install_debian
  elif is_arch; then
    install_arch
  else
    echo "ATTENZIONE: distro non riconosciuta. Continuo senza installare pacchetti di sistema."
    echo "Assicurati di avere: python3, pip, adb e (opzionale) pywebview/GTK."
    read -rp "Premi INVIO per continuare..."
  fi
else
  echo "Salto installazione pacchetti di sistema (aggiornamento)."
fi

# --- versione python ---------------------------------------------------------
PYTHON_BIN="$(command -v python3 || command -v python)"
if [ -z "$PYTHON_BIN" ]; then
  echo "ERRORE: python3 non trovato. Installalo e riprova."
  exit 1
fi

PY_MAJOR="$($PYTHON_BIN -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$($PYTHON_BIN -c 'import sys; print(sys.version_info.minor)')"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "ERRORE: serve Python 3.10 o superiore. Trovato: $PY_MAJOR.$PY_MINOR"
  exit 1
fi

echo "Python: $PYTHON_BIN ($PY_MAJOR.$PY_MINOR)"

# --- rileva aggiornamento ----------------------------------------------------
if [ -d "$APP_DIR" ] && [ -d "$VENV_DIR" ] && [ -x "$BIN_DIR/griddroid" ]; then
  UPDATE_MODE=1
  echo "Installazione esistente trovata. Modalità aggiornamento."
fi

# --- copia applicazione ------------------------------------------------------
echo
echo "Copio GridDroid in $APP_DIR ..."
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"

# Copia il contenuto della directory corrente (repo), escludendo roba inutile.
# Provo rsync; se non c'è, uso cp -r e poi elimino le cartelle/file inutili.
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude='.git' \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='build' \
    --exclude='dist' \
    --exclude='.devin' \
    --exclude='.windsurf' \
    . "$APP_DIR/"
else
  echo "rsync non disponibile, uso cp -r ..."
  cp -r . "$APP_DIR/"
  rm -rf \
    "$APP_DIR/.git" \
    "$APP_DIR/venv" \
    "$APP_DIR/.venv" \
    "$APP_DIR/build" \
    "$APP_DIR/dist" \
    "$APP_DIR/.devin" \
    "$APP_DIR/.windsurf"
  find "$APP_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$APP_DIR" -name '*.pyc' -delete 2>/dev/null || true
fi

# Rimuoviamo i binari Windows da tools/ (inutili su Linux) per risparmiare spazio
if [ -d "$APP_DIR/tools" ]; then
  find "$APP_DIR/tools" -type f \( -iname '*.exe' -o -iname '*.dll' \) -delete 2>/dev/null || true
fi

# Verifica che scrcpy-server sia presente
if [ ! -f "$APP_DIR/tools/scrcpy-server" ]; then
  echo "ERRORE: $APP_DIR/tools/scrcpy-server non trovato."
  echo "Assicurati che il repository contenga tools/scrcpy-server."
  exit 1
fi

# --- virtualenv e pacchetti python -----------------------------------------
echo
echo "Creo il virtualenv e installo le dipendenze Python..."
if [ "$UPDATE_MODE" = "1" ]; then
  echo "Riutilizzo virtualenv esistente."
else
  rm -rf "$VENV_DIR"
  $PYTHON_BIN -m venv "$VENV_DIR"
fi
# shellcheck source=/dev/null
. "$VENV_DIR/bin/activate"

pip install --upgrade pip wheel
pip install --upgrade -r "$APP_DIR/requirements-linux.txt"

# --- launcher ---------------------------------------------------------------
echo
echo "Creo il comando griddroid in $BIN_DIR ..."
mkdir -p "$BIN_DIR"

cat > "$BIN_DIR/griddroid" <<'EOF'
#!/bin/sh
set -e
INSTALL_DIR="$HOME/.local/share/griddroid"
VENV_DIR="$INSTALL_DIR/venv"
APP_DIR="$INSTALL_DIR/app"
cd "$APP_DIR"
. "$VENV_DIR/bin/activate"
exec python -m griddroid "$@"
EOF
chmod +x "$BIN_DIR/griddroid"

# --- desktop entry ----------------------------------------------------------
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/griddroid.desktop" <<'EOF'
[Desktop Entry]
Name=GridDroid
Comment=Gestione farm di dispositivi Android
Exec=griddroid
Type=Application
Terminal=false
Categories=Utility;Development;
EOF

# --- fine --------------------------------------------------------------------
echo
echo "=== Installazione completata ==="
echo "Comando: $BIN_DIR/griddroid"
echo "Percorso: $APP_DIR"
echo
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]] && [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "NOTA: $BIN_DIR non sembra essere nel tuo PATH."
  echo "Esegui: export PATH=\"$BIN_DIR:\$PATH\""
  echo "Oppure riavvia la sessione per usarlo come comando 'griddroid'."
fi
echo
echo "Collega i telefoni con Debug USB attivo e avvia:"
echo "  griddroid"
