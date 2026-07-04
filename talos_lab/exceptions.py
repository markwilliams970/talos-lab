class TalosLabError(Exception):
    """Base exception for all talos-lab errors."""


class LabExistsError(TalosLabError):
    def __init__(self, name: str):
        super().__init__(f"lab '{name}' already exists")
        self.name = name


class LabNotFoundError(TalosLabError):
    def __init__(self, name: str):
        super().__init__(f"lab '{name}' not found")
        self.name = name


class NetworkPoolExhaustedError(TalosLabError):
    def __init__(self):
        super().__init__("no free /24 subnets left in 10.10.0.0/16")


class TofuError(TalosLabError):
    def __init__(self, command: list[str], returncode: int):
        super().__init__(f"tofu {' '.join(command)} failed with exit code {returncode}")
        self.command = command
        self.returncode = returncode


class TalosctlError(TalosLabError):
    def __init__(self, command: list[str], returncode: int):
        super().__init__(f"talosctl {' '.join(command)} failed with exit code {returncode}")
        self.command = command
        self.returncode = returncode


class DhcpLeaseTimeoutError(TalosLabError):
    def __init__(self, network: str):
        super().__init__(f"timed out waiting for DHCP leases on network '{network}'")
        self.network = network


class ImageNotFoundError(TalosLabError):
    def __init__(self, message: str):
        super().__init__(message)


class ImageDownloadError(TalosLabError):
    def __init__(self, message: str):
        super().__init__(message)


class VirshError(TalosLabError):
    def __init__(self, command: list[str], returncode: int, detail: str = ""):
        message = f"virsh {' '.join(command)} failed with exit code {returncode}"
        if detail:
            message += f": {detail}"
        super().__init__(message)
        self.command = command
        self.returncode = returncode


class AddonInstallError(TalosLabError):
    def __init__(self, command: list[str], returncode: int, detail: str = ""):
        message = f"helm {' '.join(command)} failed with exit code {returncode}"
        if detail:
            message += f": {detail}"
        super().__init__(message)
        self.command = command
        self.returncode = returncode


class ClusterNotReadyError(TalosLabError):
    def __init__(self, expected_count: int, timeout_seconds: int):
        super().__init__(
            f"timed out after {timeout_seconds}s waiting for {expected_count} node(s) to go Ready"
        )
        self.expected_count = expected_count
        self.timeout_seconds = timeout_seconds
