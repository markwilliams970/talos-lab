#!/usr/bin/env bash
# Installs talos-lab for the current user:
#   - creates + seeds ~/.talos-lab (lab data root)
#   - installs the package into an isolated venv under
#     ${XDG_DATA_HOME:-~/.local/share}/talos-lab/venv
#   - links the `talos-lab` executable into ~/.local/bin
#
# Safe to re-run: reuses an existing venv and just upgrades the package.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
TALOS_LAB_HOME="${HOME}/.talos-lab"
INSTALL_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/talos-lab"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_DIR="${HOME}/.local/bin"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- sanity checks ----------------------------------------------------------

[ -f "${SCRIPT_DIR}/pyproject.toml" ] || fail "run this script from inside the talos-lab repo (pyproject.toml not found next to it)"

command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"

PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')"
if [ "${PY_OK}" != "1" ]; then
    PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    fail "python3 >= 3.10 required, found ${PY_VERSION}"
fi

# --- create ~/.talos-lab (lab data root) -------------------------------------

info "creating ${TALOS_LAB_HOME}"
mkdir -p "${TALOS_LAB_HOME}/templates" "${TALOS_LAB_HOME}/images"

# --- venv + package install --------------------------------------------------

if [ ! -d "${VENV_DIR}" ]; then
    info "creating virtualenv at ${VENV_DIR}"
    mkdir -p "${INSTALL_DIR}"
    err_log="$(mktemp)"
    # Prefer the stdlib `venv` module (standard, no extra dependency).
    # Fall back to the third-party `virtualenv` tool on hosts where
    # ensurepip isn't installed (e.g. stock Debian/Ubuntu without
    # python3-venv).
    if python3 -m venv "${VENV_DIR}" >"${err_log}" 2>&1; then
        :
    elif command -v virtualenv >/dev/null 2>&1 && virtualenv -p python3 "${VENV_DIR}" >"${err_log}" 2>&1; then
        :
    else
        cat "${err_log}" >&2
        rm -f "${err_log}"
        fail "failed to create a virtualenv. Install one of: 'pip install --user virtualenv', 'sudo apt install python3-venv' (Debian/Ubuntu), 'sudo dnf install python3-virtualenv' (Fedora). Then re-run this script."
    fi
    rm -f "${err_log}"
else
    info "reusing existing virtualenv at ${VENV_DIR}"
fi

info "installing talos-lab into the virtualenv"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet "${SCRIPT_DIR}"

# --- expose the executable ---------------------------------------------------

mkdir -p "${BIN_DIR}"
ln -sf "${VENV_DIR}/bin/talos-lab" "${BIN_DIR}/talos-lab"
info "linked ${BIN_DIR}/talos-lab -> ${VENV_DIR}/bin/talos-lab"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
        warn "${BIN_DIR} is not on your PATH."
        warn "Add this to your shell profile (~/.bashrc, ~/.zshrc, ...):"
        warn "  export PATH=\"${BIN_DIR}:\$PATH\""
        ;;
esac

# --- seed default config (version.json, vm-profiles.yaml) -------------------

info "seeding default config in ${TALOS_LAB_HOME}"
"${VENV_DIR}/bin/talos-lab" version show >/dev/null

# --- check external tool dependencies (non-fatal) ----------------------------

info "checking external dependencies"
missing=0
for tool in virsh tofu talosctl kubectl qemu-img curl zstd xz; do
    if command -v "${tool}" >/dev/null 2>&1; then
        printf '  [ok]      %s\n' "${tool}"
    else
        printf '  [missing] %s\n' "${tool}"
        missing=1
    fi
done
[ "${missing}" -eq 0 ] || warn "missing tools above -- see README.md prerequisites before running 'talos-lab create'"

echo
info "done. Try: talos-lab --help"
