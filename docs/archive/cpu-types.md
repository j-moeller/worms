# CPU types of the VMs

Where the CPU type is set, which values exist, and what each one opens for an attacker.

**Threat model:** sandbox and devbox VMs are assumed malicious and rooted. Whatever root on such a VM can reach counts as exposed.

Measured on 2026-11-05 on `worms-dev`: Intel i7-4900MQ, Proxmox VE 9.2.25, QEMU 11.1.3, kernel 7.0.14-26-pve. "Measured" means tried as root inside a VM. "QEMU reports" means asked of this node's QEMU, without booting a VM. "Source" means read in the code. The list of what was not measured is under [Not measured](#not-measured).

## In short

- **`cpu: host` is set in one place today:** `cve-search`, at `opentofu/stacks/private_internet/main.tf:114`. All 9 devbox VMs had it until 2026-11-05.
- **Only `host`, `max` and the `nested-virt` flag give a VM `/dev/kvm`.** Of the 189 CPU names QEMU lists here, only `host` and `max` offer virtualisation.
- **Team VMs now use `x86-64-v3`.** It closes `/dev/kvm`, the hardware counters and the exact CPU identity, and it is the same CPU on any node with AVX2.
- **The CPU type does not protect against everything.** This node's CPU leaks between hyperthreads (L1TF, MDS), and no CPU type changes that. Only turning hyperthreading off does. See [What no CPU type changes](#what-no-cpu-type-changes).

## Where it is set

A value travels in three steps, so these are the three places to look:

1. **OpenTofu** writes it, from the files below.
2. **Proxmox** stores it as the `cpu:` line in `/etc/pve/qemu-server/<vmid>.conf`. `qm set <vmid> --cpu <value>` changes the same line by hand, and the next `tofu apply` puts the stack's value back.
3. **QEMU** gets it as `-cpu` when the VM starts, so a change needs a VM restart.

| VMs | Set at | Value | Running now |
|---|---|---|---|
| Sandbox VMs | `opentofu/stacks/sandbox/main.tf:448` | `x86-64-v3` | None built. The plan shows this value |
| Sandbox gateway | `opentofu/stacks/sandbox/main.tf:157`, no type given | Provider default, `qemu64` | None built. The plan shows this value |
| Devbox VMs | `opentofu/stacks/devbox/main.tf:185`, through `opentofu/modules/devbox_vm/main.tf:44` | `x86-64-v3` | All 9 |
| Devbox gateways | `opentofu/modules/devbox_vm/variables.tf:31`, the default | `qemu64` | All 3 |
| SSH gateway | `opentofu/stacks/devbox/main.tf:265`, no type given | Provider default, `qemu64` | `devbox-ssh-gateway` |
| Infrastructure | `opentofu/modules/infrastructure_vm/main.tf:58`, `opentofu/modules/network_monitor_vm/main.tf:74`, no type given | Provider default, `qemu64` | `database`, `syslog`, `orchestrator`, `dashboard`, `scoring`, `logging`, `dns`, `monitor` |
| Private internet | `opentofu/stacks/private_internet/main.tf:74-122`, `cpu_type = null` | Provider default, `qemu64` | `apt-cache`, `devpi`, `docker-registry`, `manpages`, `ntp`, `wikipedia` |
| **`cve-search`** | **`opentofu/stacks/private_internet/main.tf:114`, through `opentofu/modules/private_internet_vm/main.tf:75`** | **`host`** | **VM 103** |

To see what really runs:

```sh
grep -H '^cpu:' /etc/pve/qemu-server/*.conf     # the stored value, per VM
qm showcmd <vmid> --pretty | grep -- -cpu       # what QEMU gets
cat /sys/module/kvm_intel/parameters/nested     # nesting on the node
```

What QEMU gets for the three values in use. `x86-64-v3` is Proxmox's own name for `qemu64` plus flags. `host` is the only one without `enforce`, so it never refuses to start:

```text
x86-64-v3   -cpu qemu64,+abm,+aes,+avx,+avx2,+bmi1,+bmi2,enforce,+f16c,+fma,+kvm_pv_eoi,+kvm_pv_unhalt,+movbe,+pni,+popcnt,+sse4.1,+sse4.2,+ssse3,+xsave
qemu64      -cpu qemu64,enforce,+kvm_pv_eoi,+kvm_pv_unhalt
host        -cpu host,+kvm_pv_eoi,+kvm_pv_unhalt
```

### What guards it

| Guard | Where | What it stops |
|---|---|---|
| The stack refuses | `opentofu/modules/devbox_vm/variables.tf:37` | `host` and `max` on a devbox VM or gateway |
| The tests | `test_no_team_vm_is_offered_hardware_virtualisation` in `tests/test_tofu.py` | `host`, `max`, `+vmx`, `+svm` or `nested-virt` in either stack |
| The agent | `TEAM_VM_CPU` and `_has_team_vm_cpu` in `scripts/round_agent/handlers.py` | Starting a round, or placing a dev upload, unless the node's config says exactly `cpu: x86-64-v3`. It reads the node and does not ask the VM, whose answer proves nothing |
| Nesting off on the node | `/etc/modprobe.d/worms-no-nesting.conf`, from `ansible/roles/host/host_network/tasks/main.yml` | Any VM, whatever its config says. **Written, not active:** see [Next steps](#next-steps) |

The agent checks when a worm is about to run. It does not catch a devbox VM changed by hand and then used over SSH. Nesting off on the node does.

### Who may change it

| Credential | May set `host` or `nested-virt` | May use a custom model |
|---|---|---|
| Root on the node | Yes | Yes |
| The provisioning token (`WormsProvision`, used by OpenTofu and the round agent) | Yes: it has `VM.Config.CPU` | No: it lacks `Mapping.Use` |
| The orchestrator VM's token (`WormsSandboxPower`) | No | No |
| The topology token (`WormsTopology`) | No | No |

None of these credentials is on a team VM.

### History

| When | What |
|---|---|
| 2026-10-14 | `cve-search` gets `host`, for the AVX that MongoDB 8 needs |
| 2026-11-05, 05:49-05:58 | All 9 devbox VMs set to `host` by hand with `qm set`, through an operator's root SSH login |
| 2026-11-05, 12:40 | Written into the devbox stack so that a reset keeps it (`0266ce1`), without checking what it exposed |
| 2026-11-05, 14:51-15:04 | All 9 switched to `x86-64-v3` and restarted (`c8c0b95`) |

## The options

Proxmox lists 118 names on this node. 26 of QEMU's run on this CPU, and 86 do not, because they need features it lacks. The other 6 are Proxmox's own: four `x86-64-*` models and two old aliases.

| Value | What it is | Runs on this node |
|---|---|---|
| `host` | The real CPU passed through, with everything KVM can offer | Yes |
| `max` | QEMU's "everything available". The same as `host` here | Yes |
| `x86-64-v4` | `qemu64` plus AVX-512 and everything in v3 | **No:** this CPU has no AVX-512, so the VM refuses to start |
| `x86-64-v3` | `qemu64` plus AVX, AVX2, AES, FMA, BMI, MOVBE, XSAVE | Yes. **Team VMs use this** |
| `x86-64-v2-AES` | `qemu64` plus SSE4 and AES. Proxmox's web UI default | Yes |
| `x86-64-v2` | `qemu64` plus SSE4 | Yes |
| `qemu64` | QEMU's plain 64-bit CPU. The OpenTofu provider's default here | Yes |
| `kvm64` | Like `qemu64`. Proxmox's default when a VM has no `cpu:` line | Yes |
| Vendor models | An imitation of a real CPU: `Haswell-noTSX-IBRS`, `IvyBridge-IBRS`, `EPYC` and so on | Only the older Intel ones and a few very old ones |
| `custom-<name>` | Your own model in `/etc/pve/virtual-guest/cpu-models.conf`, with any flag | Not set up. The token would need `Mapping.Use` |

Any of these can also take `flags=`, for example `cpu: x86-64-v3,flags=+pcid`. See [Flags](#flags).

## What each value opens

| Value | `/dev/kvm` and nested KVM | Hardware counters | Shows the exact CPU | The VM's own protections | Same CPU on `worms-pve` |
|---|---|---|---|---|---|
| `host` | **Open.** Measured | **Open.** Measured | **Yes** | All | No: whatever is there |
| `max` | **Open.** QEMU reports | **Open.** QEMU reports | **Yes** | All | No, and it changes with QEMU |
| Any value with `flags=+nested-virt` | **Open.** Source | As the value | As the value | As the value | As the value |
| `custom-*` with `+vmx` or `+svm` | **Open.** Source | As defined | As defined | As defined | As defined |
| Vendor model, e.g. `Haswell-noTSX-IBRS` | Closed. QEMU reports | Closed. QEMU reports | Family only | Some | Only on the same or a newer CPU family |
| **`x86-64-v3`** | **Closed. Measured** | **Closed. Measured** | **No** | None | **Yes, with AVX2** |
| `x86-64-v2-AES`, `x86-64-v2` | Closed. Same base | Closed. Same base | No | None | Yes |
| `qemu64` | Closed. Measured | Closed. Measured | No | None | Yes |
| `kvm64` | Closed. QEMU reports | Closed. QEMU reports | No | None | Yes |

With nesting off on the node, the `/dev/kvm` column would be closed for every row, because KVM then no longer offers VMX at all (source). The other columns stay as they are, so `host` would still open the counters and show the exact CPU.

### 1. Nested KVM and `/dev/kvm`

**The attack:** root on the VM starts its own hypervisor. Each of its VMX instructions traps into the node's kernel, where KVM's nested code emulates them, including nested page tables. This is large, complex code, reached directly from the VM with no QEMU in between. A bug in it can give code execution in the node's kernel. That means root on the node, every secret on it, and the power to forge any result.

`/dev/kvm` is the VM's own device. It appears because the VM's kernel sees `vmx` and loads `kvm_intel`. It is a symptom, and the VMX bit is the cause: a rooted VM could always make its own device node, as the table below shows.

| Measured as root on the VM | `host` | `x86-64-v3` |
|---|---|---|
| Raw `CPUID`, VMX bit | 1 | 0 |
| `modprobe kvm_intel` | Loads | `Operation not supported` |
| `/dev/kvm` | Exists | Absent |
| Own device node `10:232`, made with `mknod` and opened | Opens | `ENODEV` |

**Bugs of this kind so far.** All are fixed. They show the class of bug and are not a list of holes in this kernel, and this list is not complete.

| CVE | CPU | What it allowed |
|---|---|---|
| [CVE-2021-29657](https://access.redhat.com/security/cve/CVE-2021-29657) | AMD | Access to host MSRs from a guest. Project Zero [turned it into a full escape](https://projectzero.google/2021/06/an-epyc-escape-case-study-of-kvm.html) |
| [CVE-2017-12188](https://www.lkml.org/lkml/2017/10/12/243) | Intel | Overwriting host kernel memory through nested page tables. The advisory treats it as a possible escape |
| [CVE-2023-30456](https://access.redhat.com/security/cve/CVE-2023-30456) | Intel | Crashing the host, in a rare setup with EPT off |

**Why hiding the bit is enough for VMX.** In kernel v7.0, `CR4.VMXE` is a reserved bit for a guest whose CPU does not list VMX (`arch/x86/kvm/x86.h:703`, applied in `cpuid.c:453`), and `VMXON` without that bit raises an exception (`vmx/nested.c:5465`). With `nested=N`, KVM does not offer VMX to any guest (`vmx/vmx.c:8194`). Under KVM, QEMU also turns `svm` off for named models (`target/i386/kvm/kvm-cpu.c:186`), so `x86-64-v3` should be safe on an AMD node too.

**What it never changed:** the network. A nested guest's traffic leaves through the VM's own NIC, so every firewall layer still applies, and the VM keeps its CPU and memory limits.

**A two-step path that remains:** `cve-search` is on `host`. Measured there: `/dev/kvm` exists and `kvm_intel` is loaded. Its port 5000 is open from team VMs (measured from `devbox-t1-a`). It runs no team code, but an attacker who breaks into that service and gets root there has `/dev/kvm`.

### 2. Hardware performance counters

**The attack:** `host` and `max` offer the CPU's performance counters (measured on `cve-search`, and QEMU reports `pmu` on for these two only). Code can then count cache misses and branch mispredictions directly, which makes side-channel attacks easier to tune. It also adds KVM's counter emulation to what a VM can reach.

Closing this only raises the effort. `rdtsc` and `clflush`, the basic tools of a cache side channel, run on every value (measured on `qemu64`, `x86-64-v3` and `host`).

### 3. The exact CPU

**The attack:** with `host` and `max`, the VM reads the exact model (measured: `Intel(R) Core(TM) i7-4900MQ`) and `arch_capabilities`, which tells it which hardware fixes the CPU has. An attacker can pick the exploit for this CPU. On `x86-64-v3` and `qemu64` it reads `QEMU Virtual CPU version 2.5+`.

This also only raises the effort. All of them still show `GenuineIntel` (measured), and timing can give away more.

### 4. The VM's own protections

`qemu64` and the `x86-64-*` models leave out features a guest kernel uses to protect itself: `smep`, `umip`, `pcid`, `spec-ctrl`, `ssbd`, `md-clear`. Measured on `x86-64-v3`, the VM's kernel reports `mds: Vulnerable: Clear CPU buffers attempted, no microcode`. QEMU's documentation says the same of its default models: they "leave the guest OS vulnerable to various CPU hardware flaws".

**This is not a path to the node.** It weakens the VM against its own processes, and the node's protections do not depend on it. Under this threat model nothing is lost, because a team already has root on its VM.

**The cost is speed:** without `pcid`, the VM's Meltdown fix (`PTI`, which measured on) is slower. This was not measured.

### 5. A CPU type hides more than it removes

The CPU type sets what the VM is told. Most instructions run anyway. Measured from user space, with each instruction run on its own:

| Instruction | `qemu64` | `x86-64-v3` | `host` |
|---|---|---|---|
| `popcnt` | **Runs, not listed** | Runs | Runs |
| `aesenc` | **Runs, not listed** | Runs | Runs |
| `movbe` | **Runs, not listed** | Runs | Runs |
| `pdep` (BMI2) | **Runs, not listed** | Runs | Runs |
| `pclmulqdq` | **Runs, not listed** | **Runs, not listed** | Runs |
| `vpaddd` (AVX2), `vfmadd` (FMA) | Illegal instruction | Runs | Runs |
| `rdrand` | Illegal instruction | Illegal instruction | Runs |
| `rdtscp` | Illegal instruction | Illegal instruction | Runs |
| `rdfsbase` | Illegal instruction | Illegal instruction | Runs |
| `rdtsc`, `clflush` | Runs | Runs | Runs |

So do not count on a CPU type to take a feature away unless KVM enforces it. VMX is one it enforces, as shown above. A worm must not rely on this table either: only what is listed is promised.

### Flags

`flags=` on a VM accepts only Proxmox's short list (`PVE/QemuServer/CPUFlags.pm`). Any flag at all is possible only in a custom model.

| Flag | Effect | Opens |
|---|---|---|
| **`nested-virt`** | Turns on `vmx` or `svm`, for any CPU type | **Nested KVM. Never on a team VM** |
| `md-clear`, `spec-ctrl`, `ssbd`, `ibpb`, `virt-ssbd`, `amd-ssbd`, `amd-no-ssb` | Lets the VM's own kernel use these fixes | Nothing for the node |
| `pcid` | Makes the VM's Meltdown fix cheaper | Nothing for the node |
| `aes` | AES instructions | Nothing: `x86-64-v3` already has it |
| `pdpe1gb` | 1 GB pages in the guest | Nothing known |
| `hv-tlbflush`, `hv-evmcs` | Hyper-V features for Windows guests | Not relevant here, and they add emulated code |

The agent refuses every flag on a team VM, because it wants the line to say exactly `cpu: x86-64-v3`. Adding one means changing `TEAM_VM_CPU` and both stacks together, and the tests fail if they differ.

## What no CPU type changes

The CPU type does not touch these. The first row is more serious for this node than nesting was, because it needs no software bug.

| Vector | Status on this node | What closes it |
|---|---|---|
| **L1TF, and [L1TF Reloaded](https://vusec.net/projects/rain):** a guest reads what its sibling hyperthread has in the L1 cache, including the host or another guest | **`Mitigation: PTE Inversion; VMX: conditional cache flushes, SMT vulnerable`** | Hyperthreading off. See below |
| **MDS:** a guest samples CPU buffers that its sibling thread uses | **`Mitigation: Clear CPU buffers; SMT vulnerable`** | Hyperthreading off |
| [VMScape](https://www.linuxjournal.com/content/vmscape-cracking-vm-host-isolation-speculative-execution-age-how-linux-patches-respond) (CVE-2025-40300): a guest steers the branch predictor to leak QEMU's memory | `Mitigation: IBPB before exit to userspace` | The kernel, already on |
| Bugs in QEMU's devices | A VM has virtio NICs and disk, the balloon, and an IDE CD-ROM for cloud-init. No USB, display, guest agent, passthrough or raw `args` | Keeping QEMU updated, and as few devices as possible |
| Bugs in KVM's core | Reached by every VM | Keeping the kernel updated |

**L1TF Reloaded matters here.** VUSec [demonstrated it against KVM](https://vusec.net/projects/rain), leaking other customers' data on a public cloud. It combines L1TF with speculative code in the hypervisor, and it [affects Intel CPUs up to Coffee Lake](https://aws.amazon.com/blogs/security/ec2-defenses-against-l1tf-reloaded). This node's Haswell is one of those. AWS writes that it works "across logical cores that share the L1 data cache", and that cache flushing and core scheduling "do not fully mitigate guest-to-host attacks". The [kernel's L1TF page](https://docs.kernel.org/admin-guide/hw-vuln/l1tf.html) also says the flush "does not prevent the SMT problem". A devbox VM runs for days, so an attacker has the time.

This was not attempted here. What it could read depends on what runs on the sibling thread, and that can be any VM or the node itself.

Turning hyperthreading off takes effect at once and restarts nothing: `echo off > /sys/devices/system/cpu/smt/control`, and `nosmt` on the kernel command line to keep it. This node then has 4 cores instead of 8 threads. `kvm-intel.vmentry_l1d_flush=always` is the stronger flush to go with it. The effect on speed was not measured. What matters for production is the hardware of `worms-pve`, which has not been checked.

## Next steps

| Step | What it closes | What it costs | State |
|---|---|---|---|
| `x86-64-v3` on sandbox and devbox VMs | Nested KVM, the counters and the exact CPU, on team VMs | Each VM's own protections, some speed, `rdrand` and similar | **Done** |
| Nesting off on the node | `/dev/kvm` for every VM, including `cve-search` and any VM later set to `host` by hand | Stopping all 28 VMs once to reload `kvm_intel`, or a node restart | Written, waiting for a decision |
| Hyperthreading off | L1TF and MDS between threads | 8 threads become 4 cores | **Waiting for a decision** |
| `cve-search` to `x86-64-v3` | The two-step path above, even with nesting on | A restart, and a test that MongoDB 8 runs on it. It has AVX, the reason for `host` | Waiting for a decision |
| Check `worms-pve` | Whether it has AVX2, and whether it reports `SMT vulnerable` | Nothing | Before going live |

## Not measured

- **A real sandbox.** None exists. The plan shows `x86-64-v3` on every sandbox VM, and the check job stops a round with any other CPU.
- **`max`, the vendor models, `kvm64` and `x86-64-v2*` in a booted VM.** These come from QEMU's own report or from Proxmox's definition.
- **`VMXON` from a guest.** Testing it needs a kernel module on the VM. The rule was read in upstream v7.0, and Proxmox's kernel carries its own patches.
- **Nesting off.** It needs the module reloaded.
- **An AMD node.** The `svm` rule was only read in QEMU's source.
- **Whether anyone used `/dev/kvm`** during the 9 hours on the devbox VMs. KVM's nested-run counter only covers running VMs, and it was not read before they restarted.
- **Any attack itself,** and the speed of any value.

## Sources

- [Proxmox VE admin guide, CPU types](https://pve.proxmox.com/pve-docs/chapter-qm.html), and `PVE/QemuServer/CPUConfig.pm` and `CPUFlags.pm` on the node
- [QEMU, recommendations for KVM CPU model configuration](https://www.qemu.org/docs/master/system/i386/cpu.html)
- [Linux, running nested guests with KVM](https://docs.kernel.org/virt/kvm/x86/running-nested-guests.html)
- [Linux, L1TF](https://docs.kernel.org/admin-guide/hw-vuln/l1tf.html)
- [Project Zero, An EPYC escape](https://projectzero.google/2021/06/an-epyc-escape-case-study-of-kvm.html)
- [VUSec, Rain and L1TF Reloaded](https://vusec.net/projects/rain) and [AWS on L1TF Reloaded](https://aws.amazon.com/blogs/security/ec2-defenses-against-l1tf-reloaded)
- Linux v7.0 source: `arch/x86/kvm/x86.h`, `cpuid.c`, `vmx/vmx.c`, `vmx/nested.c`. QEMU source: `target/i386/kvm/kvm-cpu.c`
