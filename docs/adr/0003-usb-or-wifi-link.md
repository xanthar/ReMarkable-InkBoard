# ADR 0003 — Choose USB or Wi-Fi per connection, checked by the host key

- **Status:** accepted
- **Date:** 2026-10-27
- **Decided by:** the maintainer

## Context

At first, inkboard reached the tablet only over the USB cable (`10.11.99.1`), through the
`remarkable` host block in `~/.ssh/config` (ADR 0001). The cable should be optional:
inkboard should use the cable when it is in and the tablet's Wi-Fi when it is not, switch by
itself, say on the status chip which link is in use, and work on any Wi-Fi. The Sketch pen stream
and the Screen grab (ADR 0002) must both follow the link.

On Wi-Fi the tablet's address is not fixed, and a device that is not the tablet may hold the
address inkboard last saw. The login key has no passphrase, so it must never be offered to such a
device, and the pen stream must never be read from it.

Options considered:

1. **One pen source that asks a link object for a route before every connection.** A watcher
   thread ends the current ssh run when a better link appears; the existing restart loop then
   reconnects on it.
2. Two sources, one per link, with the hub choosing between them.
3. A second `Host remarkable-wifi` block in `~/.ssh/config` and a setting to pick the host.

## Decision

Option 1.

- **Order** (`services/link.py`, `TabletLink.resolve`), tried before every connection:
  1. USB `10.11.99.1`, only when the PC routes that address over the USB adapter and port 22
     answers;
  2. the Wi-Fi address the tablet reported for itself and the last Wi-Fi address a stream ran on,
     both kept in a local `link.json` (private LAN addresses only);
  3. `INKBOARD_WIFI_ADDRESSES`, in order;
  4. a search of the PC's own networks for the tablet's MAC, only when the user has set it
     (`INKBOARD_TABLET_MAC`; no MAC ships as a default, so a fresh install sends nothing on the
     network).
- **Every connection proves the address is the tablet.** inkboard connects to the `remarkable`
  alias with `-o HostName=<address> -o HostKeyAlias=remarkable -o StrictHostKeyChecking=yes
  -o CheckHostIP=no -o UpdateHostKeys=no` in its own argv (`sources.ssh_base_argv`). A wrong host
  key ends ssh before user authentication, so the key is never offered (proven with `-v`); such a
  Wi-Fi address is skipped for 10 minutes. The pen stream's own connection is the check; only the
  move back to USB runs one extra `echo inkboard-ok` first.
- **Watching the cable is local.** Every second the watcher asks the PC, with a UDP socket
  `connect` and `getsockname`, whether `10.11.99.1` routes over the USB adapter. Nothing is sent.
  Cable out while on USB: switch at once. Cable in while on Wi-Fi: two checks in a row and the
  host-key check, then switch.
- **The search is quiet and bounded.** It covers only private subnets of the PC's physical
  adapters up to /22, skips a subnet routed into a VPN and never runs while the USB route is up.
  It sends one UDP datagram per address to the discard port so Windows learns the MAC addresses,
  reads the neighbour cache, and contacts port 22 only on the tablet's MAC. It runs once when the
  tablet is lost, then after 5 and 15 minutes and then hourly, and resets on connect. A larger
  network, a VPN or a search that found nothing is named on the chip, never cut short silently.
- **On the tablet**, SSH over Wi-Fi uses the firmware's own switch (`rm-ssh-over-wlan on`), with
  password login turned off for the Wi-Fi listener only by a unit file that replaces the
  `dropbear-wlan@` alias (`contrib/dropbear-wlan@.service`; runbook D3b). A full verified backup
  comes first (runbook D8).

## Reasons

- One source keeps one stroke model and one status: a switch is just a reconnect, and the hub
  ends any open stroke on it (`StrokeBuilder.lost`), so a pen lifted during the gap never draws
  its hover path.
- The host-key options in the argv make the check independent of `~/.ssh/config` and stop
  inkboard writing to `known_hosts`; no second host block can drift (option 3).
- A route check sees the cable within a second at no cost to the tablet; a TCP check would leave
  over the LAN once the cable is out, because Windows then routes `10.11.99.1` there.
- The quiet search finds the tablet on a new network without a port scan, and its backoff keeps a
  sleeping tablet down to one search an hour.

## Consequences

- Port 22 on the tablet now answers on every Wi-Fi it joins, key login only. The key has no
  passphrase and now travels over the LAN as well as the cable; the host-key check protects it.
- A firmware update replaces `/etc`, so the unit file disappears while the marker in `/home`
  survives: SSH over Wi-Fi comes back **with password login** until the unit file is installed
  again (runbook after-update list).
- Windows reports a refused TCP connection only after about 2 s, so the port check waits 3 s; a
  dead address in the list costs that long.
- After a replug, Windows can take up to about 30 s to bring the USB network back; inkboard stays
  on Wi-Fi until then.
- The search uses Windows-only reads (`Get-NetAdapter` through PowerShell, `arp.exe -a`);
  elsewhere it says it is not available, and when either command fails it says which. CI covers
  it with fakes on Linux and Windows.
- `probe`, `record` and the runbook steps still use the alias as written in `~/.ssh/config`, that
  is, USB.
- Screen grabs over Wi-Fi take 0.87–1.04 s, against about 0.85 s over USB.
