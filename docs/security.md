# Security

## Do Not Commit

Never commit:

- `secrets/`;
- `.env` or any real provider credential file;
- private SSH keys;
- AmneziaWG private keys and preshared keys;
- generated client `.conf` or `.vpn` files;
- live runtime snapshots if they include local operational details;
- provider subscription URLs containing `pass=`, tokens, UUIDs, or authorization strings.
- generated provider configs such as `openwrt/lokvpn.json`.

The repository includes `.env.example` with placeholders. Real values should stay in `.env` or on the target router/server.

## Provider Scripts

Scripts under `openwrt/` must read credentials from environment variables or target-machine files. They should not contain real defaults.

Current expected variables:

- `CUDY_SSH_PASSWORD`;
- `AWG_SSH_PASSWORD_CUDY_HOME`;
- `AWG_SSH_PASSWORD_HOSTVDS_USWEST`;
- `AWG_SSH_PASSWORD_MEGAHOST_AKTAU`;
- `VPNTYPE_AUTH_DEFAULT`;
- `VPNTYPE_UUID_DEFAULT`;
- `SUB_URL`.

The local web app can also read the Cudy SSH password from `secrets/cudy_ssh_password.txt`. The `secrets/` directory is ignored by git; keep that file local to the operator machine.

## GitHub

Use a private repository until all history is checked for secrets. If a secret was committed by mistake, assume it is compromised and rotate it.

GitHub login passwords must not be embedded into remote URLs, scripts, command history, or CI settings. Use Git Credential Manager, SSH keys, or a fine-grained Personal Access Token.

## Local Web App Identity

The local control app stores only password salts and PBKDF2 password hashes in SQLite.

For ordinary VPN users, the preferred identity source is the AmneziaWG client IP. If a user reaches the panel from `10.77.0.x` and that address is bound to `users.client_ip`, the user is treated as already authenticated by the VPN.

For administrator/local access, the app can also use `HttpOnly` cookie sessions stored in the local SQLite database. This is enough for the local MVP bound to `127.0.0.1`.

Before exposing the app to LAN or the internet, add TLS/reverse proxy hardening, CSRF protection, rate limiting, and a clear deployment boundary.

## Agent Device Tokens

Public control-server agents use per-device bearer tokens. The token is shown
only once by `device-create`; SQLite stores only a PBKDF2 hash and salt.

Treat a device token like a password:

- do not commit it;
- store it only on the client device;
- revoke it with `device-revoke` if a device is lost or replaced;
- issue separate tokens for every device instead of sharing one token.

Agent APIs should be exposed only over HTTPS. The Python MVP intentionally keeps
TLS at the reverse-proxy layer, for example Caddy or nginx in front of
`127.0.0.1:8765`.
