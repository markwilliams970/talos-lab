#!/usr/bin/env bash
# Installs talos-lab for the current user:
#   - creates + seeds ~/.talos-lab (lab data root)
#   - installs the package into an isolated venv under
#     ${XDG_DATA_HOME:-~/.local/share}/talos-lab/venv
#   - links the `taloslab` executable into ~/.local/bin
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

# --- --install-dependencies mode ---------------------------------------------
#
# Installs the external tools talos-lab shells out to (README.md
# prerequisites) and exits -- does NOT install talos-lab itself.
#
# Split deliberately in two:
#   - plain system packages (virsh/libvirt, qemu-img, curl, zstd, xz) are
#     auto-installed via whatever package manager is detected
#   - tofu/talosctl/kubectl/helm are NEVER auto-installed: these are
#     version-sensitive (see TALOS VERSIONING MODEL in CLAUDE.md -- a
#     talosctl newer than the pinned Talos OS version causes silent
#     Unauthorized failures well after a "successful" bootstrap) and each
#     have their own official installers. We only print where to get them.

PKG_TOOLS=(virsh qemu-img curl zstd xz)
MANUAL_TOOLS=(tofu talosctl kubectl helm)

detect_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf >/dev/null 2>&1; then echo dnf
    elif command -v pacman >/dev/null 2>&1; then echo pacman
    else echo none
    fi
}

# Echoes the package name(s) providing $2 (a talos-lab dependency tool) on
# package manager $1, or nothing if the mapping isn't known.
pkg_name_for() {
    case "$1:$2" in
        apt:virsh) echo "libvirt-clients libvirt-daemon-system" ;;
        apt:qemu-img) echo "qemu-utils" ;;
        apt:curl) echo "curl" ;;
        apt:zstd) echo "zstd" ;;
        apt:xz) echo "xz-utils" ;;
        dnf:virsh) echo "libvirt-client libvirt-daemon-kvm" ;;
        dnf:qemu-img) echo "qemu-img" ;;
        dnf:curl) echo "curl" ;;
        dnf:zstd) echo "zstd" ;;
        dnf:xz) echo "xz" ;;
        pacman:virsh) echo "libvirt" ;;
        pacman:qemu-img) echo "qemu-img" ;;
        pacman:curl) echo "curl" ;;
        pacman:zstd) echo "zstd" ;;
        pacman:xz) echo "xz" ;;
        *) echo "" ;;
    esac
}

manual_tool_url() {
    case "$1" in
        tofu) echo "https://opentofu.org/docs/intro/install/" ;;
        talosctl) echo "https://www.talos.dev/latest/talos-guides/install/talosctl/" ;;
        kubectl) echo "https://kubernetes.io/docs/tasks/tools/#kubectl" ;;
        helm) echo "https://helm.sh/docs/intro/install/" ;;
    esac
}

install_dependencies() {
    local pm to_install tool pkg packages install_cmd reply
    pm="$(detect_pkg_manager)"

    to_install=()
    for tool in "${PKG_TOOLS[@]}"; do
        command -v "${tool}" >/dev/null 2>&1 || to_install+=("${tool}")
    done

    if [ "${#to_install[@]}" -eq 0 ]; then
        info "all package-manager-installable dependencies already present"
    elif [ "${pm}" = "none" ]; then
        warn "no supported package manager found (apt-get/dnf/pacman) -- install manually: ${to_install[*]}"
    else
        packages=()
        for tool in "${to_install[@]}"; do
            pkg="$(pkg_name_for "${pm}" "${tool}")"
            if [ -z "${pkg}" ]; then
                warn "don't know the ${pm} package name for ${tool} -- install it manually"
                continue
            fi
            # shellcheck disable=SC2206
            packages+=(${pkg})
        done

        if [ "${#packages[@]}" -gt 0 ]; then
            case "${pm}" in
                apt) install_cmd=(sudo apt-get install -y "${packages[@]}") ;;
                dnf) install_cmd=(sudo dnf install -y "${packages[@]}") ;;
                pacman) install_cmd=(sudo pacman -S --needed "${packages[@]}") ;;
            esac
            info "about to run:"
            echo "  ${install_cmd[*]}"
            if [ "${ASSUME_YES}" -ne 1 ]; then
                read -r -p "Proceed? [y/N] " reply
                case "${reply}" in
                    [yY]|[yY][eE][sS]) ;;
                    *) info "skipped package install"; packages=() ;;
                esac
            fi
            if [ "${#packages[@]}" -gt 0 ]; then
                "${install_cmd[@]}" || warn "package install failed -- see output above"
            fi
        fi
    fi

    echo
    info "the following are version-sensitive and never auto-installed -- see"
    info "README.md prerequisites for why, install yourself if missing:"
    for tool in "${MANUAL_TOOLS[@]}"; do
        if command -v "${tool}" >/dev/null 2>&1; then
            printf '  [ok]      %s\n' "${tool}"
        else
            printf '  [missing] %-10s %s\n' "${tool}" "$(manual_tool_url "${tool}")"
        fi
    done
}

INSTALL_DEPENDENCIES=0
ASSUME_YES=0
for arg in "$@"; do
    case "${arg}" in
        --install-dependencies) INSTALL_DEPENDENCIES=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h)
            cat <<EOF
Usage: $(basename "$0") [--install-dependencies] [--yes]

  --install-dependencies  Install the external tools talos-lab shells out
                          to (see README.md prerequisites) and exit --
                          does not install talos-lab itself. Packages
                          installable via apt/dnf/pacman (virsh, qemu-img,
                          curl, zstd, xz) are installed automatically;
                          version-sensitive tools (tofu, talosctl,
                          kubectl, helm) are never auto-installed -- their
                          official install links are printed instead. Run
                          this first, then re-run '$(basename "$0")' with
                          no flags to install talos-lab itself.
  --yes, -y               Don't prompt before running the package manager
                          install command. Only affects
                          --install-dependencies.
EOF
            exit 0
            ;;
        *) fail "unknown option: ${arg} (see --help)" ;;
    esac
done

if [ "${INSTALL_DEPENDENCIES}" -eq 1 ]; then
    UNAME_S="$(uname -s)"
    UNAME_M="$(uname -m)"
    case "${UNAME_S}/${UNAME_M}" in
        Linux/x86_64|Linux/amd64) ;;
        *) fail "talos-lab only supports Linux/amd64 (detected ${UNAME_S}/${UNAME_M})" ;;
    esac
    install_dependencies
    echo
    info "done. Re-run '$(basename "$0")' with no flags to install talos-lab itself."
    exit 0
fi

# --- sanity checks ----------------------------------------------------------

UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"
case "${UNAME_S}/${UNAME_M}" in
    Linux/x86_64|Linux/amd64) ;;
    *) fail "talos-lab only supports Linux/amd64 (detected ${UNAME_S}/${UNAME_M})" ;;
esac

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
ln -sf "${VENV_DIR}/bin/taloslab" "${BIN_DIR}/taloslab"
info "linked ${BIN_DIR}/taloslab -> ${VENV_DIR}/bin/taloslab"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
        warn "${BIN_DIR} is not on your PATH."
        warn "Add this to your shell profile (~/.bashrc, ~/.zshrc, ...):"
        warn "  export PATH=\"${BIN_DIR}:\$PATH\""
        ;;
esac

# --- seed default config (version.json, vm-profiles.yaml, addons.yaml) ------

info "seeding default config in ${TALOS_LAB_HOME}"
"${VENV_DIR}/bin/taloslab" version show >/dev/null
"${VENV_DIR}/bin/python3" -c "from talos_lab import addons; addons.load_addons_config()"

# --- check external tool dependencies (non-fatal) ----------------------------

info "checking external dependencies"
missing=0
for tool in virsh tofu talosctl kubectl helm qemu-img curl zstd xz; do
    if command -v "${tool}" >/dev/null 2>&1; then
        printf '  [ok]      %s\n' "${tool}"
    else
        printf '  [missing] %s\n' "${tool}"
        missing=1
    fi
done
[ "${missing}" -eq 0 ] || warn "missing tools above -- see README.md prerequisites before running 'taloslab create'"

echo
info "done. Try: taloslab --help"
