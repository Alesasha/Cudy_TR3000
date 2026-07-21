# Current Status

Snapshot date: 2026-07-21.

Baseline tag: `snapshot-2026-07-18-android-code-enrollment-1.25`.

This document records the verified live state. Planned work belongs in
`docs/roadmap.md`; historical notes are not operating instructions.

## Repository Baseline

- This source tree defines the Android mobile-admin snapshot. Local secrets,
  APKs, databases, logs and runtime artifacts remain outside version control.
- Agent stabilization and recovery are committed in `219d198`.
- `secrets/`, APKs, local databases, logs and runtime output remain ignored.
- Current agent artifacts:
  - Android `1.42 (43)` published APK; its SHA256 is recorded in
    `docs/android-agent.md` and the production update manifest;
  - Linux `1.27 (28)`, published from the current source with a manifest-verified package;
  - Windows `1.27 (28)`, SHA256
    `e4719300e06ec22b835fdef75266bbabd698c23c8ab7fa75ac387642e576b6e2`.
- The recovery checkpoint includes Cudy PBR/rollback safety, private backup/SSH
  access, bounded fallback retries and the Windows OpenAI-maintenance source.

## Primary Control Server

The `uswest` server at `95.182.91.203` is the production control-server.

Verified on 2026-07-18:

- `vpn-control.service` is enabled and active with `Restart=always`;
- `/healthz` and `/readyz` are healthy;
- Auto probe and provider refresh workers are enabled and have no worker error;
- 25 of 31 transports are enabled and no enabled transport is stale;
- 3 of 6 enabled agents are currently online; three enabled agents are
  currently offline/stale and remain an operational follow-up;
- Windows is intentionally disabled on the development workstation and the
  Linux agent is offline/stale pending the next real-world acceptance run;
- operational probe warnings are zero; nine recent failed apex probes are
  classified as suffix targets without DNS records and do not trigger retries
  for 24 hours or degrade readiness;
- direct SSH audit, restricted tunnel-user deployment and agent update
  downloads work.
- restricted tunnel-user deployment now has a sequential system OpenSSH mode;
  it completed a production code promotion despite intermittent public banner
  delays, without opening parallel SSH sessions;
- uswest now has a verified private SSH recovery path through Cudy to the host
  bridge address `172.29.172.1`; a one-minute systemd timer repairs the host
  return route through the AmneziaWG container if Docker recreates it or the
  route is removed. A deliberate route-deletion test recovered automatically
  and private SSH then passed end to end;
- the scheduled operator backup and Cudy fallback-sync tasks both have a zero
  last result. The backup task now uses the private
  `PC -> Cudy -> awg2 -> uswest` path by default; the 2026-07-18 archive passed
  member, metadata, secret-presence and SQLite integrity checks.

The control-server remains authoritative for policy, provider transport plans,
Auto cache, probe jobs, enrollment, agent updates and admin/user UI.

The 2026-07-18 production deploy uses the verified private management route
`PC -> Cudy -> awg2 -> 172.29.172.1:22`, so it no longer depends on the
intermittent public SSH banner. The production database was preserved and
contains 11 users and 4 devices. The Users page accepts agent-only users without
a web password or legacy Cudy peer. The Devices page exposes edit, enable,
disable and permanent-delete actions; row and form Save buttons remain disabled
until a field actually changes.

The private management path is reliable but slow. Controlled tests measured
about 73-76 Mbit/s between the workstation and Cudy, but only 2-5 Mbit/s through
the Cudy-uswest AWG path. Cudy remained 82-95% idle. On uswest, independent
`top` and `vmstat` samples recorded 21-91% CPU steal, TCP RTT inflation from a
170 ms minimum to roughly 700-750 ms, retransmissions and 1.85-3.9 Mbit/s
effective throughput. A 4 MiB TCP window and lower AWG MTUs did not remove the
bottleneck. This identifies the oversubscribed uswest KVM host/path as the
current deployment-speed and SSH-stability risk; contact the provider or move
the clone to a better VPS rather than tuning Cudy further.

Control-server startup no longer runs Auto scheduling or provider refresh
synchronously before opening the HTTP listener. Auto receives a five-second
startup delay and provider refresh a 30-second delay; their normal recurring
intervals are unchanged. In the production verification on 2026-07-18,
systemd started the service at `05:08:18 UTC`, HTTP began serving at
`05:08:30 UTC` instead of taking about 28 seconds, and the complete private
code-only deployment fell from 76.5 to 54.6 seconds. Auto then completed its
first cycle without error while the UI and agent API remained available. The
delayed provider cycle also finished normally, refreshing 18 transport records;
six individual provider endpoint checks failed and remain visible as provider
results rather than blocking service readiness.

Android code-only enrollment is deployed. The universal APK contains a shared
bootstrap key for the shell-less `cudy-enroll` account. sshd restricts it to
local forwarding to `127.0.0.1:8766`; that listener exposes only health and
one-time enrollment. A valid code atomically creates the device token and
unique Ed25519 key, returns the private device credential once, and disables
the code. The issued key then uses `cudy-tunnel-agent`, restricted to the normal
local control port `127.0.0.1:8765`. A production end-to-end test passed both
channels and removed its temporary user afterward. Android `1.25 (26)` also
keeps the protected mobile-admin user/device CRUD screen. The admin password is
held only for the login request and is not persisted.

Android `1.40 (41)` is published for the focused stability soak. It restores
persisted configuration when Android restarts the sticky VPN service, records
process/service/boot/recovery markers, retries a native-engine or critical-link
safety stop instead of remaining permanently off, and schedules a persisted
2-4 minute recovery-job chain. The compact main screen now exposes explicit yellow
Starting, green Connected, orange recovery/degraded, and stopping states while
routing, diagnostics, permissions, and technical settings remain collapsed.
Static UI checks, Release compilation, APK manifest inspection, production
deployment, SHA256 verification, `/healthz`, and strict `/readyz` passed. A
physical reboot, manual stop/start, process-kill recovery, foreground-service,
VPN validation, and chained recovery-job acceptance passed on the Xiaomi Mi
Note 10 Lite. A longer locked-screen/network-transition soak and repetition on
the second enrolled phone remain the acceptance gate. Version 1.40 additionally
checks the production update channel every six hours on an unmetered network,
downloads through the device's authenticated SSH channel, verifies the APK hash,
package, version and signer, and notifies the user to approve installation. The
main screen always shows installed/latest versions and manual checks remain in
an acknowledged dialog instead of flashing transient status text. The attached
physical phone completed the user-approved `1.38 -> 1.40` installation and now
reports version code 41 with the VPN service foreground and requested.

A later direct audit on 2026-07-16 reached TCP/22 but did not complete the SSH
banner/session. The Cudy restricted control tunnel and live fallback policy
continued to work. A subsequent audit received three banners, completed five
authenticated sessions and passed the full strict production check. Effective
limits are `MaxStartups 100:30:300`, `PerSourceMaxStartups 20`; the watchdog,
firewall guard and fail2ban are active, with no stale pre-auth children at the
time of inspection. Because the failure is intermittent, no control-server
maintenance should depend on a single fresh public SSH session. Timeout entries
from the home/Cudy public IP must be treated as possible agent reconnects, not
automatically as an attack.

## Cudy Fallback And Router Agent

The Cudy router is currently reachable from the primary LAN at
`192.168.1.174` and from its own LAN as `192.168.8.1`.

Verified fallback state:

- `cudy-fallback` is running;
- the restricted control tunnel is running;
- fallback readiness has zero warnings;
- policy source is live with 31 routes; the active transport plan is dynamic
  and contained 7 transports in the latest strict check;
- the observer is enabled and continues reporting fresh state.

The Go `cudy-router-agent` is intentionally back in `observe` mode with
`allow_apply=0`. It does not own PBR, DHCP or WAN routing. A stale production
configuration had left it in unguarded `apply`; the 2026-07-16 audit returned
it to observe without changing the current PBR files.

Live PBR is running again. The incident cause is confirmed: the previous
watchdog incorrectly treated `/var/run/pbr.lock`, a transient rebuild lock, as
a persistent running marker. When the lock disappeared it stopped PBR, removed
all fwmark `ip rule` entries and emptied `pbr_prerouting`. The corrected safety
scripts are deployed: they validate the real nft/ip-rule dataplane, attempt one
serialized recovery, and fail open only if recovery fails. A deliberate live
test removed all 22 PBR `ip rule` entries; the watchdog detected the loss and
restored all 22 rules in 112 seconds. Final state has 69 prerouting mark rules,
`expected=yes`, `failed=none`, and forwarding enabled. A physical Ethernet
probe confirmed Direct via `195.170.35.108`/RU for a neutral target and tunneled
ChatGPT via `45.136.59.135`/KZ.

The old AmneziaVPN/Aktau maintenance guard is disarmed. The operator workstation
now uses a dedicated standalone `OpenAI-USWest` AWG service whose endpoint is
pinned to the physical Wi-Fi path. It installs only current OpenAI `/32` routes,
has no default or `/1` routes, and the OpenAI probe currently succeeds through
`uswest`. The source now persists and self-heals the physical endpoint pin and
refreshes OpenAI routes every two minutes. The scheduled refresh task is
installed and a two-reboot test passed: Windows changed the Wi-Fi index twice,
the task reconnected profile `Sel23aw028`, selected the current adapter index,
removed the stale endpoint route and restored OpenAI without a default or `/1`
route on the AWG adapter.
A manually connected AmneziaVPN application tunnel suspends the dedicated
service to prevent nested VPN routing. Cudy management remains on Ethernet
`192.168.8.102 -> 192.168.8.1`.

PBR now reflects the generated control-server plan retained by the committed
guarded trial. The Go agent itself is back in `observe`; strict checks report no
file drift from that committed state.

The fallback and observer gates now satisfy the read-only preflight baseline:

- the fallback observer now preserves the last successful cache metadata and
  retries one bounded failed fetch. Three consecutive strict fallback checks
  passed after aligning the external timeout with its 20-second preview budget;
- an earlier router-observer series passed two of three checks; the third failed
  only Gemini through `proxyde` and requested a transport refresh. After that
  refresh, three checks spanning multiple observer cycles passed with critical
  health 5/5, zero blockers, zero warnings and zero transport actions. This
  clears the preview/probe flap for the checkpoint;
- the guarded bootstrap and override tools retain explicit apply/commit gates,
  independent timed rollback and BusyBox `start-stop-daemon` arming. Their next
  previews must be regenerated from current policy before any live trial;
- the Windows OpenAI recovery path now survives reboot independently of Cudy,
  so a failed Cudy trial cannot strand the development session.

A guarded `lokvpn-de1` transport bootstrap then completed both required stages:
the first trial rolled back automatically and restored `observe`; the second was
committed and left only the transport/PBR bootstrap in place. Five critical
services remained healthy. The subsequent provider refresh exposed a separate
control-server defect: LokVPN returns a different valid Reality `short_id` every
15 minutes while endpoint, UUID, public key, SNI and flow remain unchanged.
This creates a false perpetual `refresh-and-restart` action. The source now
selects list-valued IDs deterministically and retains the active ID when every
other transport identity field is unchanged. The change is deployed and passed
a natural provider refresh without recreating the false action. A later refresh
changed the real endpoint, port, UUID and SNI together; that legitimate transport
replacement was applied through the separately committed guarded bootstrap.

The first override-only apply attempt showed that adding a newly bootstrapped
interface to `pbr.config.supported_interface` is not enough: the required
per-interface nft user set does not exist until a full PBR rebuild includes an
override for that interface. The Go agent now checks every desired interface set
and chooses the full guarded bootstrap path when one is absent.

The next uncommitted attempt exposed two safety timing defects rather than a
policy defect. A complete PBR rebuild on this router can take over two minutes,
while the trial used a 90-second settle window. Its delayed rollback then
overlapped the rebuild, and a stale lock caused the independent watchdog to stop
an otherwise healthy PBR dataplane after 300 seconds. Direct WAN remained
available, but tunneled services such as Telegram stopped until PBR was restored.
The restart lock now records its owner PID, removes only dead locks, and clears
the in-progress marker explicitly before reporting success. The trial defaults
are now 600/420 seconds and any failed wait requests an immediate rollback. The
fixed watchdog and rebuilt PBR have remained healthy beyond the old 300-second
failure threshold. The router-agent remains in `observe`; the corrected route
trial was rerun, but its first PBR build rejected the generated fw4 include and
the independent transaction restored the legacy overrides successfully. The
new policy referenced `lokvpn-de1`, whose sing-box netdevice and PBR supported
interface entry existed but whose OpenWrt `network` interface/firewall zone did
not. OpenWrt UCI also rejects named sections containing a hyphen, and that same
hyphen produced an invalid nft identifier. The corrected model keeps the
physical TUN as `lokvpn-de1` but uses `lokvpn_de1` for OpenWrt network, firewall
and PBR state.

Transport registration is now transactional state owned by the Go agent. For
every managed sing-box interface it verifies and repairs the OpenWrt `network`
entry, firewall zone, LAN and optional `friends` forwarding/QUIC rules, and the
PBR supported-interface list. Network, firewall and PBR configs are included in
both agent rollback and the independent guarded bootstrap. A new route trial is
blocked until this registration passes, provider transport actions return to
zero and the per-interface nft set exists after a clean PBR bootstrap.

The corrected `lokvpn_de1` registration and nft set now pass independent UCI,
firewall, netdevice, PBR-list and nft checks. The first corrected uncommitted
route trial reached a healthy apply state and its on-router guard then restored
the previous overrides, `observe` mode and the closed apply gate without the
workstation. WAN recovered with 0% packet loss and the legacy Telegram exits
answered through `proxyfr` and `proxynl`. Network failures now receive three
bounded attempts while semantic/content failures are still never retried.

On 2026-07-18 the remaining Phase 5 gates passed. A legitimate `proxyfi`
`refresh-and-restart` action was applied by a separately committed transport
bootstrap. A second uncommitted route trial then reached healthy apply state;
its independent on-router timer restored the previous files, 24 PBR rules,
`observe` mode and `allow_apply=0` without workstation intervention. A separate
committed route trial also reached healthy state, retained the generated route
files and returned the agent to `observe`. Subsequent strict fallback and
observer checks report live policy, 31 routes, critical health 5/5, zero changed
files, zero blockers, zero warnings and zero transport actions at acceptance.
Phase 5 guarded apply is therefore accepted; ongoing soak remains before
DHCP/WAN migration. The latest read-only check now reports nine pending PBR
file differences caused by newer Auto winners, while blockers, warnings and
transport actions remain zero. They require another guarded apply/commit window
and are not applied automatically while the agent stays in `observe`.

An independent `cudy-main-router-guard` service is installed and running on
Cudy. It is currently disarmed and its pre-cutover configuration backup passes
SHA256 verification. When explicitly armed during a maintenance window, it
checks only structural LAN/default-route/WAN-gateway health and restores the
saved OpenWrt network, DHCP, firewall and wireless configuration if the trial
fails or is not committed before its deadline. It deliberately does not use an
external website outage as a rollback trigger.

Some router-local TUN diagnostics can produce false failures. For
`http-proxy-tun` transports the observer now probes the upstream HTTP proxy
from the root-only cached transport plan. The remaining intermittent preview
and TLS timeouts still require investigation before they can be classified as
probe artifacts or real provider failures. The agent remains in `observe` mode;
the committed generated files are active under the independently guarded PBR
dataplane.

## Android Agent

Android `1.40 (41)` is built, signed and published on the production
control-server through the private Cudy management path. The production APK
and update manifest both have SHA256
`41fb87efdb97e43bcd160f6301b24955b74ac1bb167f7c029e6da251b5d6b660`.
Fresh-device code-only enrollment and the resulting per-device control channel
passed against production. The universal APK has now been installed and
activated successfully on two physical phones. The Xiaomi acceptance phone was
kept on `1.38 (39)` to exercise the real production update path. Version `1.40 (41)`
keeps the recovery/UI work and adds verified background APK delivery; the
multi-day acceptance soak remains.

Verified acceptance:

- signed APK and production manifest SHA256 match;
- real reboot delivered `LOCKED_BOOT_COMPLETED` without reading
  credential-encrypted preferences;
- `BOOT_COMPLETED` started the foreground service after the configured network
  delay;
- SSH control, policy fetch and TUN recovered automatically;
- Android reports the VPN network as `VALIDATED`;
- `mail.ru` used `Direct`;
- `chatgpt.com` used `proxyde`;
- Telegram `149.154.160.0/20` used `proxyfr`;
- a production probe job tested `proxyde` and `proxynl` while browser traffic
  stayed active;
- probe jobs now use persistent loopback-only mixed inbounds and do not reload
  or interrupt the active TUN;
- recently probed libbox-compatible transports remain in a six-hour warm set
  bounded above the current inventory size. After the pending probe queue was
  drained, a new production probe completed with config hash
  `4D7F28AF106B` unchanged and no libbox reload;
- a forced Wi-Fi outage kept the foreground service alive; after Wi-Fi returned
  the agent recreated the TUN and Android reported the VPN `VALIDATED` again.
- the current physical-device check confirms an active
  foreground `CudyVpnService`, and a device-idle whitelist entry for
  `com.nashvpn.cudyagent`; the production package is not debuggable.
- the Administration activity opens from the main application, is not exported
  to other Android applications, and displays its credential-protected login
  surface. It supports user/device create, edit, enable/disable, delete and
  one-time enrollment over the shared restricted SSH connection.
- a real `1.38 -> 1.40` production check downloaded the 46.9 MB APK through the
  authenticated device channel, verified version, signer and SHA256, and opened
  Android's installer. Google Play Protect requires the owner to approve the
  private sideload (`Details -> Install anyway`) and then confirm biometrics;
  that final owner confirmation is the remaining step of this acceptance run.
- version 1.40 restores a cached libbox transport before the first remote policy
  fetch, reuses one persistent SSH local forward, moves periodic update work off
  Android's main thread, and avoids blocking service shutdown on an active SSH
  request. The control policy also excludes the Gosuslugi package `ru.rostel`
  from Android VpnService so its own VPN detection sees a direct connection.

Remaining Android concerns:

- Android cannot query the MIUI Autostart permission. The app records an
  explicit confirmation after the user returns from the vendor settings, while
  notification, VPN and battery permissions remain automatically verified;
- a longer locked/background soak is still required despite the current
  device-idle whitelist and successful boot recovery;
- the 1.27 UI hides raw diagnostics, routing and advanced settings by default;
  the longer-term visual redesign remains a separate UX task;
- JavaScript-only geographic decisions still require rendered probes.
- long-running acceptance on the two newly activated phones is now in progress;
- fresh-phone onboarding is self-contained: the admin UI serves the universal
  APK and creates a copyable one-time code.

## Windows Agent

- Windows `1.26 (27)` is built and published to the production update channel.
- Packaging, cached-policy fallback, emergency stop and watchdog regression
  tests exist.
- The production package now includes a responsive WinForms desktop UI and
  Desktop/Start Menu shortcuts. Status collection runs outside the UI thread;
  Start/Stop delegate to the existing elevated task and full direct-route
  rollback, diagnostics are copyable, and a completed update relaunches the
  replaced UI automatically. Source and packaged runtime smoke tests pass.
- The development workstation scheduled task is intentionally disabled.
- A universal desktop package can now activate a fresh Windows installation
  with the same one-time code used by Android. The bootstrap SSH key is pinned
  to the production host key and can forward only to the enrollment listener;
  successful enrollment stores a unique device token/key and strict
  `known_hosts` entry. Local packaging and static regression pass; a fresh
  physical Windows acceptance remains. The admin Devices workflow now serves
  this universal installer when Windows is selected.
- Normal traffic on this workstation must not depend on an unaccepted agent
  build while Codex development is active.
- A dedicated standalone `OpenAI-USWest` recovery transport is active and
  fail-open. The AmneziaVPN background service remains available for manual
  fallback, but its GUI/full-tunnel service must stay disconnected during
  normal development.
- Two controlled reboots confirmed that the endpoint bypass survives Wi-Fi
  interface reindexing. A SYSTEM task reconnects the configured Wi-Fi profile,
  pins the AWG endpoint outside Cudy and refreshes OpenAI host routes every two
  minutes. The task is intentionally not readable from a non-elevated shell,
  but its state timestamp and routes update normally.
- A 30-request HTTPS sample had no failures. The uswest path still showed high
  latency variance (roughly 200-400 ms), so a longer streaming soak and an
  alternate OpenAI maintenance exit remain useful follow-ups.

## Linux Agent

- Linux `1.27 (28)` is published.
- The wrapper now explicitly reports transport-management capability.
- The one-click package contains service install, status, diagnostics,
  rollback and bundled sing-box support.
- A universal Linux package now accepts a one-time code, obtains a unique
  device token and SSH key through the restricted enrollment listener, writes
  `agent.env` atomically, pins the control-server host key, and installs the
  service in the OFF state. Local package and shell/static regressions pass;
  fresh-machine acceptance remains. The admin Devices workflow now serves this
  universal installer when Linux is selected.
- The systemd-owned control tunnel is now the only SSH tunnel owner. The UI
  and diagnostics wait for it instead of killing and recreating the shared
  forward, while startup removes only exact orphaned forwards left by a crash.
- Transport startup no longer trusts a PID file alone. It verifies that the
  PID belongs to the expected sing-box config and that the requested TUN
  interface appears; stale/reused PIDs are discarded and a missing interface
  fails the cycle. This fixes the observed state where `health.ok=true` and
  Telegram routes were installed while `vpn_interfaces` was empty.
- Dima installed the preceding update and reported Gemini working but ChatGPT
  unavailable. The control-server showed his device stale/offline from
  `2026-07-17T12:09:24+00:00`, while the effective ChatGPT rules and current
  Auto winners were present. This isolates the immediate failure to agent
  recovery rather than a missing ChatGPT route.
- Update completion is reported only after the systemd service is active. The
  `1.27` watchdog no longer disables autostart after failed service probes:
  isolated critical-service failures are reported, while a complete base
  connectivity failure temporarily stops the service, restores direct routing
  and retries the still-enabled agent after a cooldown.
- The Linux taskbar icon now follows runtime state every five seconds: green
  healthy, yellow starting/recovery/update, red lost control and black only
  for an explicitly disabled agent.
- A long real-world test on Dima's machine is still required for suspend,
  resume, Wi-Fi changes, Zapret, UFW and update behavior.

## Auto, Policy And UI

- Policy precedence is implemented as user domain, user default, global
  domain, global default.
- Ordered candidate lists and `all-rest` are implemented.
- Probe assignment prefers a capable agent that used the domain.
- User-scoped probe jobs can now be assigned and claimed only by devices of
  that user. Telegram CIDRs use the known service endpoint
  `149.154.167.50:443` instead of probing the arbitrary first address of each
  network. Existing contaminated Telegram winners were replaced with the
  known-working `proxynl` recovery choice pending valid same-user probes.
- Default apex probes that every candidate reports as `resolve_failed` use a
  bounded 24-hour negative cache. This supports suffix routes such as
  `oaiusercontent.com` whose apex intentionally has no address while keeping a
  periodic retry.
- The background scheduler now considers at most 300 recently changed or
  promoted Auto targets per cycle. Fresh winners are skipped until their TTL
  expires; stale winners become probe-eligible again. Worker status reports the
  selected window and total target counts. Regression tests cover a 305-domain
  set and fresh/stale cache behavior; a production traffic soak is still needed
  before calling the real-usage requirement complete.
- Global and per-user aliases are isolated and tested.
- Important Service dependency groups can share one cache key, candidate list
  and winner; an isolated production staging test passed and was cleaned up.
- CIDR targets belonging to one service alias now use one grouped Auto winner.
  All eight Telegram networks therefore switch exits atomically instead of
  splitting one session across several provider countries. Dima's Telegram
  group is back in `auto`; `proxynl` is only its seeded current winner.
- Auto no longer treats known technical dependencies as standalone websites.
  Seeded service groups provide canonical probe URLs for YouTube, Gemini,
  ChatGPT/OpenAI and Reuters. Production assigned `ytimg.com` to an Android
  agent using `https://www.youtube.com/` and accepted `proxyde`; new OpenAI
  dependency jobs use `https://chatgpt.com/`. Earlier bare-apex failures remain
  visible only until the rolling failure window expires.
- Generic success/failure regexes and known geo-block content checks exist.
- Recent Auto history now returns failed jobs with per-candidate reasons,
  latency and HTTP status; the admin candidate editor displays up to three
  recent failures next to the last ten winners.
- Agent-token user entry, credential-protected admin entry, Route Lookup,
  Auto cache, agent controls and failure history were rendered and exercised
  against production on desktop and a 375-pixel mobile viewport.
- Admin and user pages have no document-level horizontal overflow or browser
  console errors; wide admin tables scroll inside their section.
- The Users and Agents tables now have client-side search with visible result
  counts. User default-server selectors omit unavailable stale servers while
  preserving an already selected unavailable value, and the web-login column
  says `configured` / `not set` instead of the ambiguous `yes` / `no`.
- Control JSON uses conditional gzip and the admin page loads its data and
  system status concurrently. The external private-Cudy status probe is cached
  for 60 seconds instead of adding a three-second timeout to every refresh.
- The status page distinguishes active/enabled transports, reports provider
  refresh counts, and labels the private Cudy check as unreachable from the VPS
  instead of incorrectly calling it stale. The independent Cudy checks remain
  authoritative for fallback health.
- SSH recovery no longer treats AWG peer `10.8.1.1` as a server management
  address. The verified private uswest management address is the Docker host
  bridge `172.29.172.1`; `cudy-awg-private-management-route.timer` maintains
  its return path to the inbound AWG subnet. Public SSH, private SSH through
  Cudy, provider-console recovery and the Cudy control fallback are now
  independent recovery layers.
- Full enrollment/update/device lifecycle usability still needs the remaining
  Phase 4 audit and automated rendered regression coverage.
- The HTTP lifecycle regression now creates and deletes a user, revokes and
  consumes one-time enrollment codes, rejects code reuse, and verifies that
  disabled/deleted device tokens fail immediately while Enable restores the
  same token. Agent token caches are invalidated on every device state change.
  Destructive actions now use explicit labels and confirmations: user deletion
  distinguishes account-only removal from legacy Cudy peer revocation, and
  device controls say `Apply state` / `Delete device`. These controls were
  rendered against production on desktop and at 375 pixels without page-level
  overflow or console errors. Automated rendered regression coverage remains.
- Reuters was absent from managed policy and therefore remained Direct. Its
  apex, `www` host and `www.reutersmedia.net` dependency are now seeded and
  deployed as global Auto routes with a `reuters` service alias. This incident
  confirms that the reviewed daily domain/IP update flow is not yet complete;
  provider refresh is automatic, but managed-domain source updates still need
  Phase 3 implementation.
- YouTube dependencies are now also seeded as global Auto routes, so a restored
  or cloned control-server retains the production policy instead of depending
  on a one-off live database edit.

## Non-Negotiable Safety Gates

- Keep `cudy-router-agent` in `observe`; any future apply must still use the
  guarded trial with independent timed rollback.
- Do not make an unguarded live Cudy PBR/transport change. `OpenAI-USWest` must
  remain available on its independent physical Wi-Fi path during guarded work.
- Do not run a guarded Cudy apply while the strict observe check reports preview
  timeouts or critical-service failures.
- Do not move DHCP or WAN ownership from AirTies to Cudy until the accepted
  guarded routing state completes its soak and the physical rollback is ready.
- Do not enable the Windows development task without the independent watchdog
  and tested `Emergency-Stop-Agent.cmd` path.
- Do not treat an HTTP 200 response as service success when content or rendered
  state indicates geographic blocking.
- Keep Cudy fallback data fresh before any `uswest` migration or maintenance.

## Immediate Next Step

Continue the Android `1.34 (35)` daily-use soak across Wi-Fi, mobile data, lock,
background update download, user-approved installation, and reboot. The fresh
Cudy main-router preflight completed with no hard failure;
prepare encrypted Cudy Wi-Fi, VLAN 2 validation and missing static
forward-target addresses before moving DHCP/WAN. The independent config
rollback guard is installed and disarmed; a physical cable/address rollback is
still mandatory. The first read-only forward-target probe found only
`192.168.1.105` present out of eight unique targets, so the other seven old
rules must not be migrated until their devices are powered on and verified.
The current snapshot confirms kernel/userspace 802.1Q support; the ISP-facing
`eth0.2` binding itself remains deliberately unapplied.
