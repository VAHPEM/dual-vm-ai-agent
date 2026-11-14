# Lab isolation — the boundary of the free-action lab

The lab VMs are a **free-action malware-analysis lab**: on them a worker may turn off
Windows Defender, add AV exclusions, change the firewall, registry, services, ACLs or
accounts, and run as root. `labhand.safety` no longer filters any of that; it refuses
only three commands that wreck a VM's disk (recursive delete of `/`, `mkfs` /
`diskpart`, `format X:`).

So what keeps a sample inside the lab is the **network and sharing setup of the VMs in
UTM**. That setup is the human's to apply, in the UTM GUI. This page records what was
measured and what to change; labhand changes nothing here.

## Measured state (2026-11-14)

Host: macOS 15.5 (24F74), UTM 4.7.4. Read from each VM's `config.plist` (read-only)
under `~/Library/Containers/com.utmapp.UTM/Data/Documents/<VM>.utm/`, and from inside
the guests through labhand.

| | Kali Linux | Windows |
|---|---|---|
| backend | QEMU (aarch64, `virt`) | QEMU (aarch64, `virt`, TPM) |
| NIC | 1 × virtio-net-pci | 1 × virtio-net-pci |
| network mode | **Shared** (vmnet NAT) | **Shared** (vmnet NAT) |
| `IsolateFromHost` | false | false |
| port forwards | none | none |
| clipboard sharing | **on** | **on** |
| directory sharing | **WebDAV, read-write** | **WebDAV, read-write** (WebClient service running) |
| USB sharing | off | off |
| guest agent | spice-vdagent running | vdservice running |
| guest address | 192.168.64.0/24, gateway + DNS 192.168.64.1 (the Mac, `bridge100`) | same subnet, same gateway + DNS |

Reachability measured from inside each guest:

| from the guest to … | Kali | Windows |
|---|---|---|
| the Internet (`https://example.com`, ICMP 1.1.1.1, DNS) | **reachable** | **reachable** |
| the Mac at 192.168.64.1, TCP 22 / 445 / 5000 / 7000 | **open** | **open** |
| the Mac's own LAN gateway (172.20.10.1) | **reachable** | **reachable** |
| the other VM | reachable | reachable |

Inside Windows at the time: Defender `RealTimeProtectionEnabled=True`, `AMServiceEnabled=True`
(WinDefend service RUNNING); Windows Firewall, current (Public) profile: **OFF**.

**What this means:** with today's setup a sample that runs in either VM can reach the
Internet (C2, downloads, spreading), the Mac's own services (SSH, SMB file sharing,
AirPlay/Control Center on 5000/7000), the rest of the Mac's LAN, and a writable folder
on the Mac through the WebDAV share; it can also read and write the Mac's clipboard.
Before running real malware with security settings turned off, this must change.

## What labhand needs from the network

Only one direction: **the Mac opens SSH to each VM** (`KALI_HOST`, `WIN_HOST` in
`.env`). labhand needs no guest-to-Mac, guest-to-Internet or VM-to-VM traffic.
Installing tools in a guest does need the Internet; do that in a separate window (see
below), never while a sample is in the VM.

## Target configuration (apply in UTM, each VM, VM shut down)

1. **Network → Network Mode: Host Only.** No NAT to the Internet or the LAN; the Mac can
   still reach the guest. Leave *Isolate Guest from Host* **off**: it would cut the
   Mac's SSH to the guest too, and labhand with it. The host-only subnet differs from
   the shared one, so the guests get new addresses: read them inside each guest and
   update `KALI_HOST` / `WIN_HOST` in `.env`.
2. **Sharing → Clipboard Sharing: off.**
3. **Sharing → Directory Sharing: none** (or at least read-only). Move files with
   labhand's `upload_file` / `download_file` instead: they go over SSH and only when the
   Mac asks; a downloaded file lands outside any git checkout (`~/labhand-downloads/`),
   mode 0600, and is never executed.
4. **Input → USB Sharing:** keep off.
5. **No port forwards** (none today; keep it so).

Host Only still lets a guest open connections **to the Mac** on the host-only bridge.
To close that too, filter guest-initiated traffic on the Mac. A sketch for macOS `pf`
(the human decides and applies it; `bridgeNNN` is the host-only bridge, check with
`ifconfig` while the VMs run):

```
# /etc/pf.anchors/labhand  — load with: sudo pfctl -a labhand -f /etc/pf.anchors/labhand
lab_if = "bridge101"                   # the host-only bridge, check with ifconfig
pass  out quick on $lab_if proto tcp to port 22 keep state   # Mac -> guest SSH (labhand)
pass  in  quick on $lab_if proto udp from any port 68 to any port 67   # guest DHCP
block in  quick on $lab_if                                  # any guest-initiated packet
```

Alternatively turn off the services the guests can reach today (System Settings →
General → Sharing: Remote Login = SSH 22, File Sharing = SMB 445, AirPlay Receiver =
5000/7000) while samples run.

## Updating tools (Internet needed)

Switch the VM to **Shared** only for an update session, with no sample inside (best:
right after reverting to a clean snapshot), then switch back to **Host Only** and
check again before the next sample.

## Check after applying

Run these through labhand (or in the guest's console). Each line must report the
blocked state:

```bash
# Kali
curl -sS -m 8 -o /dev/null -w '%{http_code}\n' https://example.com   # must FAIL
ping -c1 -W3 1.1.1.1                                                # must FAIL
for p in 22 445 5000 7000; do timeout 3 bash -c "</dev/tcp/<mac-host-only-ip>/$p" && echo "$p OPEN"; done   # no OPEN (with pf)
```

```powershell
# Windows
Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 https://example.com   # must FAIL
Test-Connection 1.1.1.1 -Count 1 -Quiet                                 # False
```

And from the Mac, labhand must still work: `shell.run_cmd("echo ok", host="kali")` and
`host="windows"` both return ok.
