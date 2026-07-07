# talos-lab

A thin orchestration CLI that turns OpenTofu + libvirt + Talos Linux into a
reproducible, disposable local Kubernetes lab — like a simplified GKE
lifecycle manager for your workstation.

talos-lab does **not** replace OpenTofu. It renders `.tf` files and shells
out to `tofu`, `talosctl`, `virsh`, and `kubectl` for every real
infrastructure operation. It only owns the bookkeeping: lab state, network
allocation, and command sequencing.

**Linux/amd64 only, for now.** talos-lab's backend is libvirt/KVM on Linux,
and `taloslab get`'s image fetch is hardcoded to the `metal-amd64` release
asset. There's no macOS support and no arm64 image handling yet — see
section 9 for status.

---

## 1. Prerequisites

**Platform requirement: Linux on amd64.** talos-lab's provisioning backend
is libvirt/KVM, and `taloslab get` only fetches `metal-amd64` Talos images.
It cannot be installed or run on macOS (or arm64) at present — there is no
macOS backend and no arm64 image support. See section 9 for the full
rationale and status of a possible future macOS port.

Install these on the Linux host before touching talos-lab:

| Tool | Purpose | Check |
|---|---|---|
| KVM / libvirt | VM hypervisor | `virsh --version` |
| [OpenTofu](https://opentofu.org) | provisions VMs + network | `tofu version` |
| [terraform-provider-libvirt](https://github.com/dmacvicar/terraform-provider-libvirt) | libvirt resources for OpenTofu | auto-installed by `tofu init` |
| [talosctl](https://www.talos.dev/latest/talos-guides/install/talosctl/) | generates configs, bootstraps the cluster | `talosctl version --client` |
| kubectl | cluster validation, kubeconfig merge | `kubectl version --client` |
| [Helm](https://helm.sh) | installs the CNI + standard add-on complement post-bootstrap (section 6) | `helm version` |
| curl | used by `taloslab get` to download the golden image | `curl --version` |
| zstd, xz | used by `taloslab get` to decompress the golden image (different Talos releases use different compression — talos-lab tries both) | `zstd --version`, `xz --version` |
| qemu-img | converts the Talos disk image to qcow2 | `qemu-img --version` |
| Python 3.10+ | runs talos-lab itself | `python3 --version` |
| python3-venv | `install.sh` creates the venv with stdlib `python3 -m venv`; on Debian/Ubuntu this module ships separately from `python3` and is often missing by default | `python3 -m venv --help` |

**Install `talosctl` before running `taloslab` for the first time.**
`taloslab` seeds its Talos-version pin (section 3a) by reading `talosctl
version --client` and matching it — this matters more than it sounds
like it should. See the callout in section 3a for why.

To install the plain system packages from the table above (KVM/libvirt,
qemu-img, curl, zstd, xz) automatically via your distro's package manager
(apt/dnf/pacman), run:

```bash
./install.sh --install-dependencies
```

This only installs those five — not talos-lab itself, and not
OpenTofu/talosctl/kubectl/Helm. Those four are deliberately left out:
they're version-sensitive (see section 3a), each has its own official
installer, and this command just prints where to get them instead of
guessing a version for you.

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
`~/.local/bin/taloslab`. It's safe to re-run — it reuses the existing venv
and just upgrades the package. If `~/.local/bin` isn't already on your
`PATH`, the script tells you what to add to your shell profile. At the end
it prints a checklist of the external tools from section 1 it found (or
didn't) on `PATH`. Missing tools? See `./install.sh --install-dependencies`
in section 1.

### Option B: manual dev install (editable, for working on talos-lab itself)

```bash
git clone <this repo> talos-lab && cd talos-lab
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
taloslab --help
```

### Uninstalling

```bash
./uninstall.sh                      # removes the executable + venv only
./uninstall.sh --purge-data         # also deletes ~/.talos-lab (prompts first)
./uninstall.sh --purge-data --yes   # same, without the prompt
```

By default `uninstall.sh` only removes the `~/.local/bin/taloslab` symlink
and the venv — it deliberately leaves `~/.talos-lab` (registry, per-lab
state, golden images) in place, since that's the only record of how to
cleanly tear down any VMs/networks you've already provisioned. If you still
have labs registered, it prints their names and tells you to `taloslab
delete` them first; deleting `~/.talos-lab` while VMs still exist orphans
them in libvirt with nothing left tracking them. Pass `--purge-data` once
you're sure, which still prompts for confirmation unless you add `--yes`.

---

## 3. Bootstrap the Talos golden image

All labs share **one** Talos version and **one** disk image (`version.json`
+ `~/.talos-lab/images/`). You must fetch that image once per Talos version
before your first `taloslab create`.

talos-lab boots VMs directly from a pre-built disk image (no PXE/ISO install
step), so it needs the **metal raw disk image**, not the installer ISO.

### 3a. Pick a Talos version

```bash
taloslab version show   # first run seeds this automatically — see below
```

The **first time** you run any taloslab command, it pins the Talos
version to whatever `talosctl version --client` reports on your machine —
you don't need to set it yourself for a normal setup. To pin something
else explicitly:

```bash
taloslab version set v1.13.5
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
> version to your installed `talosctl` (`taloslab version set` + a fresh
> `taloslab get`), not chasing the individual auth errors.

### 3b. Fetch the image

```bash
taloslab get           # fetches whatever version is currently pinned
taloslab get v1.13.5   # or fetch a specific version explicitly ("1.13.5" also works)
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

`get` also compares the version you're fetching against your installed
`talosctl` (`talosctl version --client`). If they differ, it prints a
warning and asks you to confirm before fetching — see the note above on
why that skew matters (`-y`/`--yes` skips this prompt too, but the
warning still prints).

Repeat whenever you `taloslab version set` a different Talos version —
each version gets its own image file alongside the others, and `taloslab
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
system extension for clean shutdowns under libvirt) — `taloslab get` only
fetches the stock, no-extensions image. If you download a qcow2 disk image
from the Factory (or anywhere else) by hand, use `put` instead of copying
it into place yourself:

```bash
taloslab put ~/Downloads/metal-amd64.qcow2 v1.13.5
```

`put` validates the file is actually qcow2 (via `qemu-img info`) before
copying it in, prompts before overwriting an existing image for that
version (pass `-y`/`--yes` to skip), and writes atomically the same way
`get` does. The version argument is optional and defaults to whatever's
currently pinned. If the Factory gave you a `.raw`/`.raw.zst` asset
instead of qcow2, decompress and convert it first (section 3c) — `put`
will refuse a non-qcow2 file rather than silently storing something that
won't boot.

---

## 4. Quick start

```bash
# 1. confirm the auto-detected version pin (matches your installed talosctl)
taloslab version show

# 2. fetch the matching image into ~/.talos-lab/images/
taloslab get

# 3. create a lab: 1 control plane + 2 workers
# (prompts for VM size per role since --cp-profile/--worker-profile
#  aren't given here -- see section 5)
taloslab create demo 2

# 4. list labs: VM counts/roles/sizes, Talos version, and active context
taloslab list

# 5. use it
kubectl get nodes
kubectl get pods --all-namespaces
kubectl run hello-world --restart=Never --image=hello-world -it
kubectl delete pod hello-world
taloslab use demo

# 6. check on it any time -- bootstrap stage, VM status, live cluster readiness
taloslab status demo

# 7. power the VMs off/on without destroying the lab
taloslab stop demo
taloslab start demo

# 8. tear it down for good
taloslab delete demo
```

`create` is resumable — if it fails partway (image missing, DHCP lease
timeout, a `tofu apply` error), fix the underlying issue and re-run the
exact same `taloslab create demo 2`. It picks up from the last completed
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
taloslab stop demo            # graceful shutdown (virsh shutdown), waits up to 60s
taloslab stop demo --force    # hard power-off (virsh destroy) if a node won't shut down
taloslab start demo           # boots the control plane + all workers again
```

Both commands are idempotent — starting an already-running VM or stopping
an already-stopped one is a no-op. They require the lab to have been
provisioned at least once (`taloslab create`); running them against a lab
whose VMs don't exist yet raises a clear error telling you to run `create`
first.

`taloslab stop-all` runs `stop` against every registered lab, one after
another — **run this before suspending the laptop or closing the lid.**
Host suspend doesn't gracefully shut a VM down; it just freezes the QEMU
process mid-instruction along with the rest of the host and resumes it
later, with no ACPI signal to the guest. The guest's clock doesn't advance
during that window, and etcd/Kubernetes both lean on wall-clock heartbeat
and lease timeouts, so a real suspend can trigger raft elections (or worse
on a long suspend) on resume. `stop` first avoids all of that:

```bash
taloslab stop-all             # graceful shutdown of every lab
taloslab stop-all --force     # hard power-off of every lab
```

Labs not yet provisioned are skipped (printed, not an error). Each lab is
attempted independently — one lab failing to stop doesn't block the
others — but if any lab failed, `stop-all` exits non-zero after
attempting all of them, so a pre-suspend script can detect that it's not
safe to suspend yet.

Note that Talos VMs mount their root filesystem read-only with ephemeral
state by default, so a `stop`/`start` cycle behaves like a reboot from the
cluster's perspective — etcd and kubelet state on disk persist across the
power cycle, but anything that only lived in memory does not.

**Memory pressure warning.** talos-lab is built for a laptop with finite
RAM (32-64GB, not unlimited), and each lab's VMs are sized independently
with no awareness of what else is running. Both `taloslab start` and
`taloslab create` check whether another lab already has running VMs
before booting more, and if so, warn you and show real memory numbers
before asking to continue:

```
$ taloslab start labb
warning: lab(s) already running: laba
starting another lab increases memory pressure. Current host memory:
               total        used        free      shared  buff/cache   available
Mem:            30Gi         12Gi        5.4Gi        82Mi         13Gi         18Gi
Swap:          1.9Gi        8.0Ki        1.9Gi

Start 'labb' anyway? [y/n] (n):
```

Declining leaves things exactly as they were — for `start`, the lab just
isn't started; for `create`, VM provisioning is skipped but the lab stays
registered and resumable (re-run `create` later, same as any other
partial-create interruption). Pass `--yes`/`-y` to skip the prompt (e.g.
in scripts); if no other lab is running, neither command prompts at all.

This is a nudge backed by real numbers, not a hard capacity check —
talos-lab doesn't sum up profile sizes against host memory or otherwise
try to guarantee both labs will actually fit.

---

## 4b. Listing labs

`taloslab list` prints one row per lab:

```
   lab    talos    control-plane                workers                    ready
 * demo   v1.13.5   1 x medium (4vCPU/8192MB/40GB)   2 x medium (4vCPU/8192MB/40GB)   yes
```

Everything in that row — Talos version, VM counts, roles, and resolved
CPU/memory/disk — is a **snapshot taken at `create` time**, not a live
lookup. If you later `taloslab version set` a different version or edit
`vm-profiles.yaml`, existing labs keep reporting what they were actually
built with; only the *next* `create` picks up the new values. `*` marks
whichever lab's context is your current `kubectl` context. A `--single-node`
lab (section 4c) shows `-- (single-node)` in the workers column instead of
a misleading `0 x <profile>`.

---

## 4c. Single-node labs

`taloslab create <name> 1` still creates **two** VMs — a control plane
and a worker, unchanged. For a single VM that acts as both, use
`--single-node` instead of a worker count:

```bash
taloslab create demo --single-node                    # prompts for one VM profile
taloslab create demo --single-node --cp-profile large  # skip the prompt
```

`--single-node` can't be combined with a nonzero worker count (it'll
error). Unlike a plain `taloslab create demo 0` — which also produces one
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

## 4d. Status

`taloslab status <name>` reports VM status, bootstrap progress, and live
cluster readiness for one lab — and works at any stage, including before
anything's been provisioned, which is exactly when it's most useful:

```
$ taloslab status demo
demo  talos=v1.13.5  topology=1 control-plane + 2 worker(s)  network=talos-demo

Bootstrap stage:
  done     VMs provisioned (OpenTofu)
  done     Talos config applied
  done     Cluster bootstrapped
  done     Kubeconfig retrieved + nodes Ready
  pending  Addons installed

VMs:
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ domain             ┃ role          ┃ virsh state ┃ ip          ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ demo-controlplane   │ control-plane │ running     │ 10.10.0.94  │
│ demo-worker-0       │ worker        │ running     │ 10.10.0.119 │
└────────────────────┴───────────────┴─────────────┴─────────────┘
Cluster:
  talos-k9o-1fs: Ready
  talos-9i7-qu3: Ready

kube context: talos-lab-demo (active)
```

If VMs are stopped or a node hasn't joined yet, the cluster section shows
`unreachable` rather than hanging — this is a single quick check (10s
timeout), not the same polling wait `create` does.

`taloslab status-all` runs this for every registered lab, one after
another with a divider between them — useful for a quick "what's running
right now" sweep across all your labs.

---

## 5. VM sizing

Profiles live in `~/.talos-lab/templates/vm-profiles.yaml` (seeded on first
run, yours to edit):

```yaml
micro:  { cpu: 1, memory: 2048,  disk: 10 }
small:  { cpu: 2, memory: 4096,  disk: 20 }
medium: { cpu: 4, memory: 8192,  disk: 40 }
large:  { cpu: 8, memory: 16384, disk: 80 }
```

Add/edit profiles freely — profile names are never hardcoded anywhere in
talos-lab, everything reads this file directly. Note this file is only
*seeded* once on first install; adding a profile to the shipped default
later (as `micro` was added here) won't retroactively appear in an
existing `~/.talos-lab/templates/vm-profiles.yaml` — add it there by hand
too if you want it on an already-installed system.

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
taloslab create demo 2 --cp-profile large --worker-profile small
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
- Each `/24` is split so MetalLB (section 6a) always has a static range
  DHCP will never hand to a VM: `.1` gateway, `.2`–`.199` DHCP (far more
  than any lab's realistic node count), `.200`–`.250` reserved for
  MetalLB, `.251`–`.254` unused buffer. This split only applies to labs
  created after it was introduced — older labs keep whatever DHCP range
  Terraform actually applied for them at the time (see 6a).

---

## 6a. Add-ons

After bootstrap, `create` installs a CNI (mandatory) plus a standard
add-on complement (optional) via Helm, both driven by
`~/.talos-lab/templates/addons.yaml` (seeded on first run, yours to edit
— same pattern as `vm-profiles.yaml` in section 5).

**CNI (always installed, not optional):** Talos's built-in Flannel CNI has
no `NetworkPolicy` support, so talos-lab always disables it
(`cluster.network.cni.name: "none"` in the generated machine config) and
installs [Cilium](https://cilium.io) instead. Nodes report `NotReady`
until this succeeds — a CNI is a hard requirement for a functioning
cluster, not a nice-to-have, so there's no toggle for it.

**CoreDNS:** Talos deploys its own CoreDNS automatically at bootstrap and
talos-lab doesn't replace it — `addons.yaml`'s `coredns.image` only
overrides the image Talos's own CoreDNS deployment uses; leave it blank
to use Talos's default.

**Standard add-on complement (optional, on by default):**

| Add-on | Purpose |
|---|---|
| metrics-server | resource metrics for `kubectl top` |
| cert-manager | TLS certificate automation |
| kube-state-metrics | cluster object state metrics |
| ingress-nginx | HTTP(S) ingress controller |
| MetalLB | LoadBalancer IPs on this bare libvirt network (backs ingress-nginx's `Service`) |

Toggle any of these off (`enabled: false`) or repin `chart_version`/`values`
in `addons.yaml` — no code change needed either way.

MetalLB always installs first, regardless of where it's listed in
`addons.yaml` — ingress-nginx's chart defaults its Service to
`type: LoadBalancer`, and if MetalLB isn't there yet to assign an IP,
`ingress-nginx`'s install hangs until it times out. talos-lab also labels
the `metallb-system` namespace `pod-security.kubernetes.io/enforce=privileged`
before installing it — Talos's default cluster-wide Pod Security policy
(`enforce: baseline`, applied to every namespace except `kube-system`)
otherwise rejects MetalLB's `speaker` DaemonSet outright, since it needs
`hostNetwork`/`hostPort`/`NET_ADMIN` to do L2 announcement. Both of these
were found by actually booting a lab, not just written speculatively —
see the note at the end of this section.

`create` prompts to skip *all* of them (CNI is unaffected) when the
control-plane or worker VM profile is `micro`/`small`, since they carry
real memory overhead that can crowd out workloads on a node that size:

```
note: this lab's VM profile is small enough that the standard add-on
complement can meaningfully crowd out workloads:
  - metrics-server       resource metrics for `kubectl top`
  - cert-manager         TLS certificate automation
  - kube-state-metrics   cluster object state metrics
  - ingress-nginx        HTTP(S) ingress controller
  - metallb              LoadBalancer IPs on this bare libvirt network
Cilium (the cluster's CNI) is always installed regardless of this choice
-- NetworkPolicy support isn't optional.
Install the standard add-on complement? [y/n] (y):
```

This is a one-time decision made at `create` time (like `--single-node` or
the VM profile), stored per-lab and reused on a resumed `create` — it
won't re-prompt or silently change your answer on retry. `--yes` skips the
prompt and installs the complement (the default answer).

Labs created before this feature existed don't have a reserved MetalLB
range (see section 6's DHCP/MetalLB split) — for those, everything else
installs normally but MetalLB is skipped with a warning; recreate the lab
to get MetalLB support.

**Verified against a real cluster:** a full `taloslab create` run (Cilium,
CoreDNS, and all five standard add-ons) has been confirmed working
end-to-end — both nodes `Ready`, every addon pod `Running`, `ingress-nginx`
actually getting a MetalLB-assigned `EXTERNAL-IP`, and a `NetworkPolicy`
actually blocking traffic it should block (the reason for swapping off
Flannel in the first place). Getting there surfaced three real, non-obvious
issues along the way: Cilium's default chart values request a capability
(`SYS_MODULE`) Talos's kernel doesn't support; the CNI install needed a
retry around the apiserver's brief post-bootstrap unreachability; and the
MetalLB ordering/Pod-Security fixes described above. All three are already
fixed in `addons.yaml`/the installer itself — nothing you need to do.

---

## 6b. Security

**If you're coming from a managed cloud offering like GKE, expect this
cluster to feel stricter out of the box.** Every lab runs with Talos
Linux's built-in [Pod Security Admission](https://kubernetes.io/docs/concepts/security/pod-security-admission/)
(PSA) enforcement active, unconditionally, from the moment the cluster
exists.

Kubernetes's PSA controller checks pods against three standard levels:
`privileged` (no restrictions), `baseline` (blocks known
privilege-escalation paths — `privileged: true`, `hostNetwork`/`hostPID`/
`hostIPC`, `hostPath` volumes, dangerous capabilities like `SYS_ADMIN`/
`NET_ADMIN`), and `restricted` (baseline plus requiring `runAsNonRoot`,
dropped capabilities, `allowPrivilegeEscalation: false`, an explicit
`seccompProfile`). Each level has independent `enforce` (reject),
`audit` (log), and `warn` (client warning) modes.

Talos's own default, unconditionally applied to every namespace except
`kube-system`:

```yaml
enforce: baseline    # actually rejects violating pods
audit:   baseline    # talos-lab lowers this from Talos's own "restricted"
warn:    baseline    # talos-lab lowers this from Talos's own "restricted"
```

**Why stricter than GKE:** this is a genuine philosophy difference, not
an oversight on either side. Talos's whole pitch is a minimal, immutable,
API-driven OS that's secure by default — no shell, no package manager,
and (as part of that same posture) PSA enforcement baked in
unconditionally. GKE Standard mode takes the opposite stance: the PSA
controller is present but enforces nothing by default — you must
explicitly label a namespace to opt into `baseline`/`restricted`. GKE
serves a huge range of pre-existing customer workloads that predate PSA,
so defaulting to enforcement there would break clusters on creation; GKE
leaves that decision to the platform team. (GKE **Autopilot** is the
exception — closer in spirit to Talos's default.) Neither is "wrong" —
they're just optimizing for different things: turnkey compatibility vs.
turnkey hardening.

**What this means in practice:** `enforce: baseline` is what actually
matters — `audit`/`warn` never block anything, they only produce log
lines or a client-side warning (which is what talos-lab silences by
lowering them to match `enforce`, since it was pure noise for a local
lab). Baseline itself is permissive for ordinary application workloads —
web servers, databases, batch jobs, anything that doesn't touch the host
— those need zero changes. What **does** get hard-rejected (a real `403
Forbidden`, not a warning) in any namespace other than `kube-system`:
anything needing `hostNetwork`/`hostPID`/`hostPath`/privileged mode or
dangerous capabilities — CNI plugins, node-level monitoring/log
collector DaemonSets (Prometheus's `node-exporter`, Fluentd/Fluent Bit),
some CSI storage node plugins, privileged CI runners doing
Docker-in-Docker.

talos-lab already hits this once, in-tree: MetalLB's `speaker` DaemonSet
needs `hostNetwork`/`hostPort`/`NET_ADMIN` for L2 announcement, so
`create` labels the `metallb-system` namespace
`pod-security.kubernetes.io/enforce=privileged` *before* installing it
(section 6a). If you deploy something similar yourself (a log collector,
`node-exporter`, etc.), apply the same pattern to its namespace *before*
installing — a DaemonSet controller won't retroactively retry pods that
were already rejected once you relabel after the fact:

```bash
kubectl create namespace <ns>
kubectl label namespace <ns> \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/audit=privileged \
  pod-security.kubernetes.io/warn=privileged
```

**Cluster-wide opt-out:** if you don't want baseline enforcement at all
for a given lab, pass `--permissive` to `create`:

```bash
taloslab create demo 2 --permissive
```

This sets the cluster's PSA default to `privileged` everywhere — actually
matching GKE Standard's real default (nothing enforced, any pod admitted,
you own workload security) instead of Talos's stricter one. If you omit
the flag, `create` prompts once at first creation (default **No** —
Enter keeps Talos's stricter baseline):

```
note: permissive mode sets this cluster's Pod Security Admission to
"privileged" cluster-wide (GKE Standard's default posture) -- any pod is
admitted, and workloads you deploy are YOUR responsibility to secure, not
the platform's. Talos's own default (baseline enforcement) is stricter
than this and is what you get by declining.
Install this cluster in permissive PSA mode? [y/n] (n):
```

Like `--single-node` and the add-on opt-out, this is a one-time decision
made at `create` time, snapshotted per-lab, and reused on a resumed
`create` — it won't re-prompt or silently change your answer. `--yes`
without `--permissive` answers **No** here (unlike the add-ons prompt,
where `--yes` answers "install" — for a security-posture choice, the sane
default under `--yes` is the stricter one, not the looser one).
`taloslab status <lab_name>` shows `psa=permissive` or
`psa=enforced (baseline)` so you can tell which mode a lab is running in
without guessing. There's no way to flip an existing lab between modes —
recreate it if you need to change this later.

This is a real reduction in cluster safety net, not just a convenience
toggle: with `--permissive`, nothing stops a workload (intentional or
not — a misconfigured manifest, a Helm chart's default values, a
compromised image) from using `hostNetwork`, mounting the host
filesystem via `hostPath`, or running privileged, in *any* namespace, not
just ones you've deliberately opted in. Use it for throwaway/exploratory
labs where PSA friction isn't worth it, not just to make one stubborn
workload's rejection go away (a namespace label, above, is the narrower
fix for that).

---

## 7. Kubeconfig behavior

- All labs merge into the single `~/.kube/config`.
- Each lab registers a context named `talos-lab-<lab_name>`.
- `taloslab create` sets the new lab as your current context.
- `taloslab use <name>` switches contexts.
- `taloslab delete <name>` removes the cluster/user/context entries and
  clears `current-context` if it pointed at the deleted lab.

---

## 8. Troubleshooting

**`error: Talos image for vX.Y.Z not found at ...`**
Run `taloslab get vX.Y.Z` (or `taloslab get` if that's already your
pinned version), then re-run `create`.

**`error: failed to download ... (exit 22)` from `taloslab get`**
The asset 404'd — the version tag likely doesn't exist or its asset name
changed. The error prints the exact URL and release-tag page it tried;
see section 3c for the manual fallback.

**`error: missing required tool(s) on PATH: ...` from `taloslab get`**
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
callout in section 3a. Fix: `taloslab version set <version matching your
talosctl>`, `taloslab get`, then `taloslab delete <lab>` and recreate
(a lab already bootstrapped under mismatched versions can't be repaired
in place — its PKI material was already generated wrong).

**`timed out waiting for N node(s) to go Ready`**
Bootstrap succeeded and the API server came up, but not every expected
node joined and went `Ready` within the timeout (~3 min). Check what's
actually happening on the slow node — this is often a node that's still
booting (check `virsh console`, same as the DHCP-timeout entry above) or
a networking issue between nodes (Cilium pods stuck — check `cni_installed`
in `taloslab status`). `kubectl get
nodes`/`get pods -A` against the lab's kubeconfig
(`~/.talos-lab/<lab>/kubeconfig`) will show which node(s) are missing or
NotReady.

**Permission denied talking to libvirt**
Your user isn't in the `libvirt` group, or you haven't re-logged-in since
adding it. Re-check with `virsh -c qemu:///system list`.

---

## 9. Known v1 limitations

- **Linux/amd64 only.** The provisioning backend is libvirt/KVM, and
  `taloslab get` only fetches the `metal-amd64` release asset — there's no
  macOS backend and no arm64 image support. A macOS/Lima-based backend has
  been scoped out in design discussion but is deliberately on hold pending
  one unresolved question: whether a Talos arm64 metal image actually boots
  under Lima's `vz` virtualization path on Apple Silicon. Nothing has been
  built for it yet.
- Single control-plane node only (no HA control plane yet) — this is
  separate from `--single-node` (section 4c), which is about one VM
  serving both roles, not about control-plane redundancy.
- The standard add-on complement (section 6a) is a fixed list
  (metrics-server, cert-manager, kube-state-metrics, ingress-nginx,
  MetalLB) — there's no way to add a wholly different addon to the
  prompted/toggleable set without editing `addons.yaml` by hand.
- Memory pressure protection (section 4a) only checks whether another
  lab's VMs are running, not actual host memory usage or whether the
  profiles chosen would actually fit — it's a nudge, not a capacity check.

---

## 10. Prior art

Other write-ups and projects covering local Talos/Kubernetes labs, for
reference:

- [Talos on libvirt/KVM (gist)](https://gist.github.com/cyrenity/67469dce33cf4eb4483486637c06d7be) —
  a manual walkthrough using `talosctl` and `virsh` to hand-build a cluster.
- [k8s-talos-local](https://github.com/0xOthmane/k8s-talos-local) — Talos on
  Docker with an ArgoCD/Cloudflare-tunnel dev stack layered on top.

---

## License

[MIT](LICENSE)
