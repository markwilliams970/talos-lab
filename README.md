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
| qemu-img | converts the Talos disk image to qcow2 | `qemu-img --version` |
| Python 3.10+ | runs talos-lab itself | `python3 --version` |

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

---

## 3. Bootstrap the Talos golden image

All labs share **one** Talos version and **one** disk image (`version.json`
+ `~/.talos-lab/images/`). You must fetch that image once per Talos version
before your first `talos-lab create`.

talos-lab boots VMs directly from a pre-built disk image (no PXE/ISO install
step), so you need the **nocloud raw disk image**, not the installer ISO.

### 3a. Pick a Talos version

```bash
talos-lab version set v1.7.6
talos-lab version show   # confirm
```

### 3b. Get the image — two options

**Option A: Talos Image Factory (recommended)**

The [Image Factory](https://factory.talos.dev) lets you pick extensions
(e.g. `qemu-guest-agent`, useful for clean shutdowns under libvirt) and
generates a matching download URL for you. Steps:

1. Open https://factory.talos.dev in a browser.
2. Select your Talos version (must match what you set in step 3a).
3. Under "System Extensions", add `siderolabs/qemu-guest-agent` (optional
   but recommended for libvirt labs).
4. Copy the generated **schematic ID** and use it in the download URL:

   ```bash
   SCHEMATIC_ID="<paste from factory.talos.dev>"
   TALOS_VERSION="v1.7.6"
   curl -LO "https://factory.talos.dev/image/${SCHEMATIC_ID}/${TALOS_VERSION}/nocloud-amd64.raw.xz"
   ```

**Option B: Stock image from the Talos GitHub release**

No extensions, fastest path to a working lab:

```bash
TALOS_VERSION="v1.7.6"
curl -LO "https://github.com/siderolabs/talos/releases/download/${TALOS_VERSION}/nocloud-amd64.raw.xz"
```

> Asset names occasionally change between Talos releases. If the URL 404s,
> browse https://github.com/siderolabs/talos/releases/tag/${TALOS_VERSION}
> and grab whichever `nocloud-amd64.raw.*` asset is listed there.

### 3c. Decompress and convert to qcow2

libvirt volumes in talos-lab are qcow2. Convert the raw image:

```bash
xz -d nocloud-amd64.raw.xz            # -> nocloud-amd64.raw
qemu-img convert -O qcow2 nocloud-amd64.raw nocloud-amd64.qcow2
```

### 3d. Place it where talos-lab expects it

talos-lab looks for exactly:

```
~/.talos-lab/images/talos-<version>-nocloud-amd64.qcow2
```

```bash
mkdir -p ~/.talos-lab/images
mv nocloud-amd64.qcow2 ~/.talos-lab/images/talos-v1.7.6-nocloud-amd64.qcow2
qemu-img info ~/.talos-lab/images/talos-v1.7.6-nocloud-amd64.qcow2   # sanity check
```

Repeat step 3 whenever you `talos-lab version set` a different Talos
version — each version needs its own image file alongside the others.

---

## 4. Quick start

```bash
# 1. pin a Talos version (once)
talos-lab version set v1.7.6

# 2. fetch the matching image (section 3) into ~/.talos-lab/images/

# 3. create a lab: 1 control plane + 2 workers
talos-lab create demo 2

# 4. list labs and see which kube context is active
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

## 5. VM sizing

Profiles live in `~/.talos-lab/templates/vm-profiles.yaml` (seeded on first
run, yours to edit):

```yaml
small:  { cpu: 2, memory: 4096,  disk: 20 }
medium: { cpu: 4, memory: 8192,  disk: 40 }
large:  { cpu: 8, memory: 16384, disk: 80 }
```

Choose per-role profiles at create time:

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
You haven't completed section 3 for the currently pinned version, or the
filename doesn't match `talos-<version>-nocloud-amd64.qcow2` exactly.

**`tofu apply` fails referencing the `default` pool**
The libvirt storage pool doesn't exist yet — see the pool setup snippet in
section 1.

**`timed out waiting for DHCP leases on network ...`**
The VM likely failed to boot the image (bad/corrupt qcow2, or you converted
an ISO instead of the nocloud raw disk). Check with:

```bash
virsh list --all
virsh console talos-<lab_name>-controlplane
```

**Permission denied talking to libvirt**
Your user isn't in the `libvirt` group, or you haven't re-logged-in since
adding it. Re-check with `virsh -c qemu:///system list`.

---

## 9. Known v1 limitations

- Single control-plane node only (no HA control plane yet).
- Kubernetes addon installation (`metrics-server`, `ingress-nginx`,
  `cert-manager`, etc.) is tracked in state (`addons_installed`) but not
  yet wired up — install manually via `kubectl`/`helm` against the lab's
  context for now.
- `talos-lab status <lab>` is not yet implemented; use `talos-lab list` and
  `virsh`/`kubectl` directly in the meantime.
