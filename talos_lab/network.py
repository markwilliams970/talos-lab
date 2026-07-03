"""Per-lab libvirt network allocation and DHCP lease discovery.

talos-lab owns address-pool bookkeeping (which /24 belongs to which
lab) so it is inspectable in Python instead of being implicit in
Terraform state. libvirt itself still hands out the DHCP leases;
we just poll `virsh net-dhcp-leases` for the result.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

from talos_lab import state
from talos_lab.exceptions import DhcpLeaseTimeoutError, NetworkPoolExhaustedError

POOL_BASE = "10.10"
MAX_SUBNETS = 256  # 10.10.0.0/16 split into /24s


@dataclass
class LabNetwork:
    name: str
    index: int
    cidr: str
    gateway: str
    dhcp_start: str
    dhcp_end: str


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
                dhcp_end=f"{POOL_BASE}.{index}.254",
            )
    raise NetworkPoolExhaustedError()


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
