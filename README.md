# talos-lab

A thin orchestration CLI that turns OpenTofu + libvirt + Talos Linux into a
reproducible, disposable local Kubernetes lab — like a simplified GKE
lifecycle manager for your workstation.

talos-lab does **not** replace OpenTofu. It renders `.tf` files and shells
out to `tofu`, `talosctl`, `virsh`, and `kubectl` for every real
infrastructure operation. It only owns the bookkeeping: lab state, network
allocation, and command sequencing.

---

## 1. Prerequisites

Install these on the Linux host before touching talos-lab:

| Tool | Purpose | Check |
|---|---|---|
| KVM / libvirt | VM hypervisor | `virsh --version` |
| [OpenTofu](https://opentofu.org) | provisions VMs + network | `tofu version` |
| [terraform-provider-libvirt](https://github.com/dmacvicar/terraform-provider-libvirt) | libvirt resources for OpenTofu | auto-installed by `tofu init` |
| [talosctl](https://www.talos.dev/latest/talos-guides/install/talosctl/) | generates configs, bootstraps the cluster | `talosctl version --client` |
| kubectl | cluster validation, kubeconfig merge | `kubectl version --client` |
| curl | used by `talos-lab get` to download the golden image | `curl --version` |
| zstd, xz | used by `talos-lab get` to decompress the golden image (different Talos releases use different compression — talos-lab tries both) | `zstd --version`, `xz --version` |
| qemu-img | converts the Talos disk image to qcow2 | `qemu-img --version` |
| Python 3.10+ | runs talos-lab itself | `python3 --version` |

**Install `talosctl` before running `talos-lab` for the first time.**
`talos-lab` seeds its Talos-version pin (section 3a) by reading `talosctl
version --client` and matching it — this matters more than it sounds
like it should. See the callout in section 3a for why.

Make sure your user can talk to libvirt without `sudo`:

```bash
sudo usermod -aG libvirt "$USER"
# log out/in, then confirm:
virsh -c qemu:///system list --all
```

talos-lab creates VM disks in the **default** libvirt storage pool. Confirm
it exists and is active:

```bash
virsh pool-list --all
# if "default" is missing:
virsh pool-define-as default dir --target /var/lib/libvirt/images
virsh pool-autostart default
virsh pool-start default
```

---

## 2. Install talos-lab

### Option A: install script (recommended for end users)

```bash
git clone <this repo> talos-lab && cd talos-lab
./install.sh
```

This creates `~/.talos-lab` (lab data), installs talos-lab into an isolated
venv under `~/.local/share/talos-lab/venv`, and symlinks the executable to
`~/.local/bin/talos-lab`. It's safe to re-run — it reuses the existing venv
and just upgrades the package. If `~/.local/bin` isn't already on your
`PATH`, the script tells you what to add to your shell profile. At the end
it prints a checklist of the external tools from section 1 it found (or
didn't) on `PATH`.

### Option B: manual dev install (editable, for working on talos-lab itself)

```bash
git clone <this repo> talos-lab && cd talos-lab
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
talos-lab --help
```

### Uninstalling

```bash
./uninstall.sh                      # removes the executable + venv only
./uninstall.sh --purge-data         # also deletes ~/.talos-lab (prompts first)
./uninstall.sh --purge-data --yes   # same, without the prompt
```

By default `uninstall.sh` only removes the `~/.local/bin/talos-lab` symlink
and the venv — it deliberately leaves `~/.talos-lab` (registry, per-lab
state, golden images) in place, since that's the only record of how to
cleanly tear down any VMs/networks you've already provisioned. If you still
have labs registered, it prints their names and tells you to `talos-lab
delete` them first; deleting `~/.talos-lab` while VMs still exist orphans
them in libvirt with nothing left tracking them. Pass `--purge-data` once
you're sure, which still prompts for confirmation unless you add `--yes`.

---

## 3. Bootstrap the Talos golden image

All labs share **one** Talos version and **one** disk image (`version.json`
+ `~/.talos-lab/images/`). You must fetch that image once per Talos version
before your first `talos-lab create`.

talos-lab boots VMs directly from a pre-built disk image (no PXE/ISO install
step), so it needs the **metal raw disk image**, not the installer ISO.

### 3a. Pick a Talos version

```bash
talos-lab version show   # first run seeds this automatically — see below
```

The **first time** you run any talos-lab command, it pins the Talos
version to whatever `talosctl version --client` reports on your machine —
you don't need to set it yourself for a normal setup. To pin something
else explicitly:

```bash
talos-lab version set v1.13.5
```

> **Why the version pin matters more than it looks like it should:** a
> `talosctl` client meaningfully newer than the Talos OS version running
> in your VMs causes real, hard-to-diagnose failures — not just a warning.
> `talosctl gen config` uses the *client's* schema, so a newer client emits
> config fields an older node's OS doesn't recognize
> (`apply-config` fails with "unknown keys found during decoding"). Worse:
> even once that's worked around, the client's certificate/PKI generation
> logic is also newer, and can produce cluster bootstrap material the
> older node's kubelet/apiserver/etcd don't handle correctly — which
> doesn't fail loudly, it surfaces later as a cascade of `Unauthorized`
> errors between kubelet, apiserver, and scheduler well after `create`
> reports success. If you ever see that, the fix is matching the Talos OS
> version to your installed `talosctl` (`talos-lab version set` + a fresh
> `talos-lab get`), not chasing the individual auth errors.

### 3b. Fetch the image

```bash
talos-lab get           # fetches whatever version is currently pinned
talos-lab get v1.13.5   # or fetch a specific version explicitly ("1.13.5" also works)
```

This downloads the `metal-amd64.raw.<ext>` asset from the matching
[siderolabs/talos GitHub release](https://github.com/siderolabs/talos/releases)
(trying `.zst` then `.xz` — different Talos releases use different
compression), decompresses it, converts it to qcow2 with `qemu-img`, and
writes it to exactly where talos-lab expects it:

```
~/.talos-lab/images/talos-<version>.qcow2
```

If an image already exists for that version, `get` **prompts before
overwriting** it (pass `-y`/`--yes` to skip the prompt, e.g. in scripts).
The write is atomic — if the download or conversion fails partway, the
existing image (if any) is left untouched.

Repeat whenever you `talos-lab version set` a different Talos version —
each version gets its own image file alongside the others, and `talos-lab
list` shows you which version each existing lab was actually built with
(see section 4b).

### 3c. If the download fails

Talos release asset **naming is not stable across versions** — e.g. older
releases published a `nocloud-amd64.raw.xz` asset that's gone entirely in
newer ones, leaving only `metal-amd64.raw.zst`. If `get` fails, it prints
every URL it tried; check
https://github.com/siderolabs/talos/releases/tag/\<version\> for the
actual current asset name, then fall back to the manual path (adjust the
extension/tool to whatever that release actually has):

```bash
curl -LO "https://github.com/siderolabs/talos/releases/download/<version>/metal-amd64.raw.zst"
zstd -d metal-amd64.raw.zst      # or: xz -d metal-amd64.raw.xz
qemu-img convert -O qcow2 metal-amd64.raw ~/.talos-lab/images/talos-<version>.qcow2
```

The [Talos Image Factory](https://factory.talos.dev) is also worth knowing
about if you need a customized image (e.g. with the `qemu-guest-agent`
system extension for clean shutdowns under libvirt) — `talos-lab get` only
fetches the stock, no-extensions image.

---

## 4. Quick start

```bash
# 1. confirm the auto-detected version pin (matches your installed talosctl)
talos-lab version show

# 2. fetch the matching image into ~/.talos-lab/images/
talos-lab get

# 3. create a lab: 1 control plane + 2 workers
# (prompts for VM size per role since --cp-profile/--worker-profile
#  aren't given here -- see section 5)
talos-lab create demo 2

# 4. list labs: VM counts/roles/sizes, Talos version, and active context
talos-lab list

# 5. use it
kubectl get nodes
talos-lab use demo

# 6. power the VMs off/on without destroying the lab
talos-lab stop demo
talos-lab start demo

# 7. tear it down for good
talos-lab delete demo
```

`create` is resumable — if it fails partway (image missing, DHCP lease
timeout, a `tofu apply` error), fix the underlying issue and re-run the
exact same `talos-lab create demo 2`. It picks up from the last completed
stage instead of starting over.

`create` doesn't report "ready" until it's actually confirmed the expected
number of nodes have joined the cluster and gone `Ready` (polling `kubectl
get nodes` against the lab's own kubeconfig, not just checking that
kubeconfig was fetchable). `kubectl get nodes`/`get pods` work immediately
once `create` exits — you shouldn't need to wait or retry after that.

---

## 4a. Start / stop

`start` and `stop` power the VMs on/off directly via `virsh` — they never
touch Terraform state, Talos config, or your kube context. Use them to free
up host RAM/CPU between sessions without re-provisioning:

```bash
talos-lab stop demo            # graceful shutdown (virsh shutdown), waits up to 60s
talos-lab stop demo --force    # hard power-off (virsh destroy) if a node won't shut down
talos-lab start demo           # boots the control plane + all workers again
```

Both commands are idempotent — starting an already-running VM or stopping
an already-stopped one is a no-op. They require the lab to have been
provisioned at least once (`talos-lab create`); running them against a lab
whose VMs don't exist yet raises a clear error telling you to run `create`
first.

Note that Talos VMs mount their root filesystem read-only with ephemeral
state by default, so a `stop`/`start` cycle behaves like a reboot from the
cluster's perspective — etcd and kubelet state on disk persist across the
power cycle, but anything that only lived in memory does not.

---

## 4b. Listing labs

`talos-lab list` prints one row per lab:

```
   lab    talos    control-plane                workers                    ready
 * demo   v1.7.6   1 x medium (4vCPU/8192MB/40GB)   2 x medium (4vCPU/8192MB/40GB)   yes
```

Everything in that row — Talos version, VM counts, roles, and resolved
CPU/memory/disk — is a **snapshot taken at `create` time**, not a live
lookup. If you later `talos-lab version set` a different version or edit
`vm-profiles.yaml`, existing labs keep reporting what they were actually
built with; only the *next* `create` picks up the new values. `*` marks
whichever lab's context is your current `kubectl` context. A `--single-node`
lab (section 4c) shows `-- (single-node)` in the workers column instead of
a misleading `0 x <profile>`.

---

## 4c. Single-node labs

`talos-lab create <name> 1` still creates **two** VMs — a control plane
and a worker, unchanged. For a single VM that acts as both, use
`--single-node` instead of a worker count:

```bash
talos-lab create demo --single-node                    # prompts for one VM profile
talos-lab create demo --single-node --cp-profile large  # skip the prompt
```

`--single-node` can't be combined with a nonzero worker count (it'll
error). Unlike a plain `talos-lab create demo 0` — which also produces one
VM, but leaves the default Kubernetes control-plane taint in place so
nothing schedules on it — `--single-node` patches the generated Talos
config (`cluster.allowSchedulingOnControlPlanes: true`) so the node is
actually usable, and labels it with both roles:

```
$ kubectl get nodes
NAME            STATUS   ROLES                 AGE   VERSION
talos-vzi-nu1   Ready    control-plane,worker   1m    v1.36.2
```

Good for a minimal footprint on a laptop, or quickly testing something
that doesn't need multiple nodes.

---

## 5. VM sizing

Profiles live in `~/.talos-lab/templates/vm-profiles.yaml` (seeded on first
run, yours to edit):

```yaml
small:  { cpu: 2, memory: 4096,  disk: 20 }
medium: { cpu: 4, memory: 8192,  disk: 40 }
large:  { cpu: 8, memory: 16384, disk: 80 }
```

Choose per-role profiles at create time with `--cp-profile`/`--worker-profile`.
Omit either one and `create` prompts for it interactively instead, showing
every profile's actual cpu/memory/disk (not just the name — "medium" alone
doesn't tell you what you're about to boot):

```
Control-plane VM profile:
  small    2 vCPU / 4096MB / 20GB
  medium   4 vCPU / 8192MB / 40GB  (default)
  large    8 vCPU / 16384MB / 80GB
Control-plane profile [small/medium/large] (medium):
```

Passing the flag skips the prompt for that role — useful for scripting:

```bash
talos-lab create demo 2 --cp-profile large --worker-profile small
```

---

## 6. Networking

Each lab gets its own isolated, NAT'd libvirt network — no sharing across
labs:

- Network name: `talos-<lab_name>`
- Subnet: the next free `/24` out of `10.10.0.0/16` (`10.10.0.0/24`,
  `10.10.1.0/24`, …), tracked in `~/.talos-lab/registry.json`
- DHCP enabled; talos-lab discovers node IPs by polling
  `virsh net-dhcp-leases talos-<lab_name>` after VM creation

---

## 7. Kubeconfig behavior

- All labs merge into the single `~/.kube/config`.
- Each lab registers a context named `talos-lab-<lab_name>`.
- `talos-lab create` sets the new lab as your current context.
- `talos-lab use <name>` switches contexts.
- `talos-lab delete <name>` removes the cluster/user/context entries and
  clears `current-context` if it pointed at the deleted lab.

---

## 8. Troubleshooting

**`error: Talos image for vX.Y.Z not found at ...`**
Run `talos-lab get vX.Y.Z` (or `talos-lab get` if that's already your
pinned version), then re-run `create`.

**`error: failed to download ... (exit 22)` from `talos-lab get`**
The asset 404'd — the version tag likely doesn't exist or its asset name
changed. The error prints the exact URL and release-tag page it tried;
see section 3c for the manual fallback.

**`error: missing required tool(s) on PATH: ...` from `talos-lab get`**
Install whichever of `curl`/`zstd`/`xz`/`qemu-img` is missing (section 1).

**`tofu apply` fails referencing the `default` pool**
The libvirt storage pool doesn't exist yet — see the pool setup snippet in
section 1.

**`timed out waiting for DHCP leases on network ...`**
The VM booted but its network never came up (or it never booted at all).
talos-lab configures a serial console on every domain, so check it first —
this is almost always faster than guessing:

```bash
virsh list --all
virsh console talos-<lab_name>-controlplane   # Ctrl+] to exit
```

Two specific things to look for in that console output:
- `x86_64 microarchitecture level 2 or higher is required, halting` — your
  host's virtualized CPU is too old for this Talos version. talos-lab sets
  `host-passthrough` CPU mode by default specifically to avoid this; if
  you've customized the libvirt domain config, that's the setting to check.
- Nothing at all, VM shows `running` in `virsh list` but the console is
  silent and stays silent — check `virsh dumpxml <domain> | grep driver`
  for the disk device; it should say `type='qcow2'`, not `type='raw'`. A
  qcow2 volume being read as a raw disk boots nothing, silently.

**`kubectl`/`talosctl` show `Unauthorized` errors after `create` reports success**
This is a `talosctl` client vs. Talos OS version mismatch — see the
callout in section 3a. Fix: `talos-lab version set <version matching your
talosctl>`, `talos-lab get`, then `talos-lab delete <lab>` and recreate
(a lab already bootstrapped under mismatched versions can't be repaired
in place — its PKI material was already generated wrong).

**`timed out waiting for N node(s) to go Ready`**
Bootstrap succeeded and the API server came up, but not every expected
node joined and went `Ready` within the timeout (~3 min). Check what's
actually happening on the slow node — this is often a node that's still
booting (check `virsh console`, same as the DHCP-timeout entry above) or
a networking issue between nodes (flannel/CNI pods stuck). `kubectl get
nodes`/`get pods -A` against the lab's kubeconfig
(`~/.talos-lab/<lab>/kubeconfig`) will show which node(s) are missing or
NotReady.

**Permission denied talking to libvirt**
Your user isn't in the `libvirt` group, or you haven't re-logged-in since
adding it. Re-check with `virsh -c qemu:///system list`.

---

## 9. Known v1 limitations

- Single control-plane node only (no HA control plane yet) — this is
  separate from `--single-node` (section 4c), which is about one VM
  serving both roles, not about control-plane redundancy.
- Kubernetes addon installation (`metrics-server`, `ingress-nginx`,
  `cert-manager`, etc.) is tracked in state (`addons_installed`) but not
  yet wired up — install manually via `kubectl`/`helm` against the lab's
  context for now.
- `talos-lab status <lab>` is not yet implemented; use `talos-lab list` and
  `virsh`/`kubectl` directly in the meantime.
