# Current Status

Snapshot date: 2026-07-17.

Snapshot tag: `snapshot-2026-07-16-android-1.21`.

This document records the verified live state. Planned work belongs in
`docs/roadmap.md`; historical notes are not operating instructions.

## Repository Baseline

- The tagged snapshot is pushed to GitHub. The current recovery checkpoint
  consolidates the post-snapshot safety work without tracking local secrets or
  runtime artifacts.
- Android `1.21 (22)` is committed in `4c97412`.
- `secrets/`, APKs, local databases, logs and runtime output remain ignored.
- Current published artifacts:
  - Android `1.21 (22)`, SHA256
    `2846b6385e4c0117e1cfdac050d925f56efa4643bdf46d3497bd556b998fc977`;
  - Linux `1.22 (23)`, SHA256
    `c5ac57f3644427c2d9251d01c6f375142c25d66c27fa3859ea9c5ace66852423`;
  - Windows `1.20 (21)`, SHA256
    `8dba7836dbd9172445e7df8af2647116cddc62bc4bd1cbb0588ba5dad8f1b6d8`.
- The recovery checkpoint includes Cudy PBR/rollback safety, private backup/SSH
  access, bounded fallback retries and the Windows OpenAI-maintenance source.

## Primary Control Server

The `uswest` server at `95.182.91.203` is the production control-server.

Verified on 2026-07-16:

- `vpn-control.service` is enabled and active with `Restart=always`;
- `/healthz` and `/readyz` are healthy;
- Auto probe and provider refresh workers are enabled and have no worker error;
- 25 of 31 transports are enabled and no enabled transport is stale;
- 2 of 4 enabled agents are currently online: Cudy and Android;
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
  last result; the latest pulled backup archive is dated 2026-07-16.

The control-server remains authoritative for policy, provider transport plans,
Auto cache, probe jobs, enrollment, agent updates and admin/user UI.

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
- policy source is live with 22 routes; the active transport plan is dynamic
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

PBR currently reflects the restored legacy Cudy override policy, not the
latest control-server Auto plan. The current control policy differs from two
live override files. Policy/transport synchronization therefore remains a
separate guarded apply step.

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
answered through `proxyfr` and `proxynl`. A later apply cycle did record two
consecutive transient ChatGPT TLS handshake timeouts through `proxyde`; network
failures now receive three bounded attempts while semantic/content failures are
still never retried. A second uncommitted soak is required before a committed
route trial.

Some router-local TUN diagnostics can produce false failures. For
`http-proxy-tun` transports the observer now probes the upstream HTTP proxy
from the root-only cached transport plan. The remaining intermittent preview
and TLS timeouts still require investigation before they can be classified as
probe artifacts or real provider failures. User traffic and live PBR remain
unchanged. The agent is still in `observe` mode.

## Android Agent

Android `1.21 (22)` is published and installed on the physical MIUI phone.

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
- the current physical-device check confirms version `1.21 (22)`, an active
  foreground `CudyVpnService`, and a device-idle whitelist entry for
  `com.nashvpn.cudyagent`; the production package is not debuggable.

Remaining Android concerns:

- a longer locked/background soak is still required despite the current
  device-idle whitelist and successful boot recovery;
- the current UI is functional but exposes too much technical state and needs
  a clearer user-facing status/version/update design;
- JavaScript-only geographic decisions still require rendered probes.

## Windows Agent

- Windows `1.20 (21)` is published and its production manifest/hash match.
- Packaging, cached-policy fallback, emergency stop and watchdog regression
  tests exist.
- The development workstation scheduled task is intentionally disabled.
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

- Linux `1.22 (23)` is published.
- The wrapper now explicitly reports transport-management capability.
- The one-click package contains service install, status, diagnostics,
  rollback and bundled sing-box support.
- The systemd-owned control tunnel is now the only SSH tunnel owner. The UI
  and diagnostics wait for it instead of killing and recreating the shared
  forward, while startup removes only exact orphaned forwards left by a crash.
- Transport startup no longer trusts a PID file alone. It verifies that the
  PID belongs to the expected sing-box config and that the requested TUN
  interface appears; stale/reused PIDs are discarded and a missing interface
  fails the cycle. This fixes the observed state where `health.ok=true` and
  Telegram routes were installed while `vpn_interfaces` was empty.
- The latest Dima report predates `1.22 (23)` and has an empty interface list,
  so production acceptance still requires installation/reconnect and a fresh
  non-empty interface report.
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

## Non-Negotiable Safety Gates

- Do not enable `cudy-router-agent` apply outside the guarded trial with its
  independent timed rollback. Repeated observe checks are not yet green.
- Do not make another live Cudy PBR/transport change until `OpenAI-USWest` has
  passed a reboot/soak check, its endpoint still bypasses Cudy over Wi-Fi, and
  the AmneziaVPN application tunnel is absent.
- Do not run a guarded Cudy apply while the strict observe check reports preview
  timeouts or critical-service failures.
- Do not move DHCP or WAN ownership from AirTies to Cudy before both guarded
  apply trials pass.
- Do not enable the Windows development task without the independent watchdog
  and tested `Emergency-Stop-Agent.cmd` path.
- Do not treat an HTTP 200 response as service success when content or rendered
  state indicates geographic blocking.
- Keep Cudy fallback data fresh before any `uswest` migration or maintenance.

## Immediate Next Step

Have Dima install/reconnect Linux `1.22 (23)` and require a fresh report with
non-empty `vpn_interfaces`, working Telegram and Reuters. In parallel, implement
the reviewed daily domain/IP source update. Do not run another Cudy route trial
until platform routing is stable and the existing guarded rollback baseline is
reconfirmed.
