#!/usr/bin/env bash
# Uninstalls talos-lab:
#   - removes the ~/.local/bin/talos-lab symlink
#   - removes the venv + package under
#     ${XDG_DATA_HOME:-~/.local/share}/talos-lab
#
# ~/.talos-lab (lab data: registry, per-lab tofu/talos state, golden
# images) is left in place by default -- it's the only record of how to
# cleanly tear down any VMs/networks you've already provisioned. Pass
# --purge-data to remove it too; that's destructive and prompts for
# confirmation unless --yes is also given.
set -euo pipefail

TALOS_LAB_HOME="${HOME}/.talos-lab"
INSTALL_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/talos-lab"
BIN_LINK="${HOME}/.local/bin/talos-lab"

PURGE_DATA=0
ASSUME_YES=0

for arg in "$@"; do
    case "${arg}" in
        --purge-data) PURGE_DATA=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h)
            cat <<EOF
Usage: $(basename "$0") [--purge-data] [--yes]

  --purge-data   Also delete ${TALOS_LAB_HOME} (lab data: registry,
                 per-lab tofu/talos state, golden images). DESTRUCTIVE --
                 if you still have provisioned labs, their VMs/networks
                 are orphaned in libvirt with nothing left tracking them.
                 Run 'talos-lab delete <name>' for each lab first.
  --yes, -y      Don't prompt for confirmation. Only affects --purge-data;
                 removing the executable/venv never prompts.
EOF
            exit 0
            ;;
        *)
            echo "unknown option: ${arg} (see --help)" >&2
            exit 1
            ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }

# --- warn about any still-registered labs, regardless of --purge-data -------

if [ -f "${TALOS_LAB_HOME}/registry.json" ] && command -v python3 >/dev/null 2>&1; then
    labs="$(python3 -c "
import json
try:
    with open('${TALOS_LAB_HOME}/registry.json') as f:
        print('\n'.join(json.load(f).get('labs', {}).keys()))
except Exception:
    pass
")"
    if [ -n "${labs}" ]; then
        warn "the following labs are still registered:"
        while IFS= read -r lab; do warn "  - ${lab}"; done <<<"${labs}"
        warn "run 'talos-lab delete <name>' for each before uninstalling, or their"
        warn "VMs/networks will keep running in libvirt with nothing tracking them."
    fi
fi

# --- remove the executable symlink -------------------------------------------

if [ -e "${BIN_LINK}" ] || [ -L "${BIN_LINK}" ]; then
    rm -f "${BIN_LINK}"
    info "removed ${BIN_LINK}"
else
    info "${BIN_LINK} not found, nothing to remove"
fi

# --- remove the venv/install dir ---------------------------------------------

if [ -d "${INSTALL_DIR}" ]; then
    rm -rf "${INSTALL_DIR}"
    info "removed ${INSTALL_DIR}"
else
    info "${INSTALL_DIR} not found, nothing to remove"
fi

# --- optionally purge lab data ------------------------------------------------

if [ "${PURGE_DATA}" -eq 1 ]; then
    if [ -d "${TALOS_LAB_HOME}" ]; then
        if [ "${ASSUME_YES}" -ne 1 ]; then
            read -r -p "Permanently delete ${TALOS_LAB_HOME} (registry, lab state, golden images)? [y/N] " reply
            case "${reply}" in
                [yY]|[yY][eE][sS]) ;;
                *)
                    info "leaving ${TALOS_LAB_HOME} in place"
                    echo
                    info "talos-lab executable/venv removed; lab data kept."
                    exit 0
                    ;;
            esac
        fi
        rm -rf "${TALOS_LAB_HOME}"
        info "removed ${TALOS_LAB_HOME}"
    else
        info "${TALOS_LAB_HOME} not found, nothing to purge"
    fi
else
    info "leaving ${TALOS_LAB_HOME} in place (lab data + golden images). Pass --purge-data to remove it too."
fi

echo
info "talos-lab uninstalled."
