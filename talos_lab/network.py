"""Per-lab libvirt network allocation and DHCP lease discovery.

talos-lab owns address-pool bookkeeping (which /24 belongs to which
lab) so it is inspectable in Python instead of being implicit in
Terraform state. libvirt itself still hands out the DHCP leases;
we just poll `virsh net-dhcp-leases` for the result.
"""

from __future__ import annotations

import random
import re
import subprocess
import time
from dataclasses import dataclass

from talos_lab import state
from talos_lab.exceptions import DhcpLeaseTimeoutError, NetworkPoolExhaustedError

POOL_BASE = "10.10"
MAX_SUBNETS = 256  # 10.10.0.0/16 split into /24s

# Each lab's /24 is split so MetalLB (see addons.py) always has a static
# range that libvirt's DHCP server will never hand to a VM: .1 gateway,
# .2-.199 DHCP (far more than any lab's realistic node count), .200-.250
# reserved for MetalLB's L2Advertisement pool, .251-.254 left unused as a
# buffer. This only affects labs created after this split existed --
# already-provisioned labs keep whatever DHCP range Terraform actually
# applied for them.


@dataclass
class LabNetwork:
    name: str
    index: int
    cidr: str
    gateway: str
    dhcp_start: str
    dhcp_end: str
    # None only for labs registered before the MetalLB pool split existed
    # -- their real Terraform-applied DHCP range still spans the whole
    # /24, so there's no static range left to hand MetalLB safely.
    metallb_pool_start: str | None
    metallb_pool_end: str | None


def allocate_network(lab_name: str) -> LabNetwork:
    used = state.used_network_indices()
    for index in range(MAX_SUBNETS):
        if index not in used:
            return LabNetwork(
                name=f"talos-{lab_name}",
                index=index,
                cidr=f"{POOL_BASE}.{index}.0/24",
                gateway=f"{POOL_BASE}.{index}.1",
                dhcp_start=f"{POOL_BASE}.{index}.2",
                dhcp_end=f"{POOL_BASE}.{index}.199",
                metallb_pool_start=f"{POOL_BASE}.{index}.200",
                metallb_pool_end=f"{POOL_BASE}.{index}.250",
            )
    raise NetworkPoolExhaustedError()


def generate_mac() -> str:
    """Generates a MAC under 52:54:00, the QEMU/KVM locally-administered
    OUI libvirt itself uses for auto-assigned guest MACs. Assigned once
    per VM at lab-registration time and persisted, so we can wait for its
    DHCP lease by MAC without depending on any Terraform-computed output.
    """
    tail = (random.randint(0, 0xFF) for _ in range(3))
    return "52:54:00:%02x:%02x:%02x" % tuple(tail)


_LEASE_LINE_RE = re.compile(
    r"(?P<mac>[0-9a-f]{2}(:[0-9a-f]{2}){5})\s+\S+\s+(?P<ip>\d+\.\d+\.\d+\.\d+)"
)


def get_dhcp_leases(network_name: str) -> dict[str, str]:
    """Returns {mac_address: ip_address} for current leases on a network."""
    result = subprocess.run(
        ["virsh", "net-dhcp-leases", network_name],
        capture_output=True,
        text=True,
        check=True,
    )
    leases: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = _LEASE_LINE_RE.search(line.lower())
        if match:
            leases[match.group("mac")] = match.group("ip")
    return leases


def wait_for_leases(
    network_name: str,
    macs: list[str],
    timeout_seconds: int = 180,
    poll_interval_seconds: int = 5,
) -> dict[str, str]:
    """Blocks until every MAC in `macs` has a DHCP lease, returns mac->ip."""
    wanted = {m.lower() for m in macs}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        leases = get_dhcp_leases(network_name)
        if wanted.issubset(leases.keys()):
            return {mac: leases[mac] for mac in wanted}
        time.sleep(poll_interval_seconds)
    raise DhcpLeaseTimeoutError(network_name)
