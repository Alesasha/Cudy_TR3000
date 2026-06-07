#!/bin/sh
set -eu

PBR_SCRIPT="${PBR_SCRIPT:-/usr/share/pbr/pbr.user.opencck-merged-vpn}"
CHECK_SCRIPT="/root/check-pbr-switch.sh"

if [ ! -f "$PBR_SCRIPT" ]; then
    echo "Missing PBR user script: $PBR_SCRIPT" >&2
    exit 1
fi

if ! grep -q "^TARGET_INTERFACE=" "$PBR_SCRIPT"; then
    echo "TARGET_INTERFACE line was not found in $PBR_SCRIPT" >&2
    exit 1
fi

backup="$PBR_SCRIPT.bak-switchers-$(date +%Y%m%d-%H%M%S)"
cp "$PBR_SCRIPT" "$backup"
echo "Backup created: $backup"

cat > /usr/bin/vpn-switch <<'EOF'
#!/bin/sh
set -eu

PBR_SCRIPT="${PBR_SCRIPT:-/usr/share/pbr/pbr.user.opencck-merged-vpn}"
TARGET_TABLE="${TARGET_TABLE:-inet fw4}"
CLEAN="${CLEAN:-/tmp/pbr_opencck_merged_vpn.clean}"
WAN_CLEAN="${WAN_CLEAN:-/tmp/pbr_wan.clean}"
FORCE_AWG2_CLEAN="${FORCE_AWG2_CLEAN:-/tmp/pbr_force_awg2.clean}"
INTERFACE_FORCE_PREFIX="${INTERFACE_FORCE_PREFIX:-/tmp/pbr_force_}"
LAN_SUBNETS="${LAN_SUBNETS:-192.168.8.0/24 10.77.0.0/24}"
DEFAULT_CHANNELS="${DEFAULT_CHANNELS:-awg1 awg2 lokvpn vpntype}"
TARGET_INTERFACE="${1:-}"

is_safe_iface() {
    case "$1" in
        ''|*[!A-Za-z0-9_.-]*) return 1 ;;
        *) return 0 ;;
    esac
}

configured_channels() {
    {
        printf '%s\n' $DEFAULT_CHANNELS
        if command -v uci >/dev/null 2>&1; then
            uci -q get pbr.config.supported_interface 2>/dev/null | tr ' ' '\n' || true
        fi
    } | while read -r iface; do
        is_safe_iface "$iface" && printf '%s\n' "$iface"
    done | awk '!seen[$0]++'
}

pbr_supported_channels() {
    if command -v uci >/dev/null 2>&1; then
        uci -q get pbr.config.supported_interface 2>/dev/null | tr ' ' '\n' || true
    fi
}

usage() {
    echo "Usage: vpn-switch INTERFACE" >&2
    echo "Known interfaces:" >&2
    configured_channels | sed 's/^/  /' >&2
}

if ! is_safe_iface "$TARGET_INTERFACE"; then
    usage
    exit 2
fi

if ! configured_channels | grep -qx "$TARGET_INTERFACE"; then
    echo "Unknown VPN interface: $TARGET_INTERFACE" >&2
    usage
    exit 2
fi

case "$TARGET_INTERFACE" in
    wan)
        echo "Use PBR force-wan lists for direct WAN; vpn-switch is for tunnel interfaces." >&2
        exit 2
        ;;
esac

target_set="pbr_${TARGET_INTERFACE}_4_dst_ip_user"

if ! pbr_supported_channels | grep -qx "$TARGET_INTERFACE" \
    && ! nft list set $TARGET_TABLE "$target_set" >/dev/null 2>&1; then
    echo "VPN interface is not registered in PBR yet: $TARGET_INTERFACE" >&2
    echo "Create the OpenWrt interface first, then add it to pbr.config.supported_interface." >&2
    exit 1
fi

if [ ! -f "$PBR_SCRIPT" ]; then
    echo "Missing PBR user script: $PBR_SCRIPT" >&2
    exit 1
fi

if ! grep -q "^TARGET_INTERFACE=" "$PBR_SCRIPT"; then
    echo "TARGET_INTERFACE line was not found in $PBR_SCRIPT" >&2
    exit 1
fi

append_nft_add_elements() {
    nftset="$1"
    file="$2"
    batch="$3"
    [ -s "$file" ] || return 1

    n=0
    params=""
    while read -r p; do
        [ -z "$p" ] && continue
        params="${params:+$params, }$p"
        n=$((n + 1))
        if [ "$n" -ge 200 ]; then
            echo "add element $TARGET_TABLE $nftset { $params }" >> "$batch"
            params=""
            n=0
        fi
    done < "$file"

    [ -z "$params" ] || echo "add element $TARGET_TABLE $nftset { $params }" >> "$batch"
}

append_interface_overrides() {
    batch="$1"
    for file in "${INTERFACE_FORCE_PREFIX}"*.clean; do
        [ -s "$file" ] || continue
        name="${file##*/}"
        iface="${name#pbr_force_}"
        iface="${iface%.clean}"
        case "$iface" in
            wan|vpn|awg2) continue ;;
        esac
        is_safe_iface "$iface" || continue
        set_name="pbr_${iface}_4_dst_ip_user"
        nft list set $TARGET_TABLE "$set_name" >/dev/null 2>&1 || continue
        append_nft_add_elements "$set_name" "$file" "$batch"
    done
}

flush_client_conntrack() {
    if command -v conntrack >/dev/null 2>&1; then
        for subnet in $LAN_SUBNETS; do
            conntrack -D -s "$subnet" >/dev/null 2>&1 || true
        done
    fi
}

sed -i "s/^TARGET_INTERFACE=.*/TARGET_INTERFACE='$TARGET_INTERFACE'/" "$PBR_SCRIPT"

if [ -s "$CLEAN" ] && nft list set $TARGET_TABLE "$target_set" >/dev/null 2>&1; then
    batch="/tmp/vpn-switch.$$.$TARGET_INTERFACE.nft"
    : > "$batch"
    for iface in $(configured_channels); do
        set_name="pbr_${iface}_4_dst_ip_user"
        if nft list set $TARGET_TABLE "$set_name" >/dev/null 2>&1; then
            echo "flush set $TARGET_TABLE $set_name" >> "$batch"
        fi
    done
    append_nft_add_elements "$target_set" "$CLEAN" "$batch"
    if [ -s "$WAN_CLEAN" ]; then
        echo "flush set $TARGET_TABLE pbr_wan_4_dst_ip_user" >> "$batch"
        append_nft_add_elements pbr_wan_4_dst_ip_user "$WAN_CLEAN" "$batch"
    fi
    if [ -s "$FORCE_AWG2_CLEAN" ] && [ "$TARGET_INTERFACE" != "awg2" ] \
        && nft list set $TARGET_TABLE pbr_awg2_4_dst_ip_user >/dev/null 2>&1; then
        append_nft_add_elements pbr_awg2_4_dst_ip_user "$FORCE_AWG2_CLEAN" "$batch"
    fi
    append_interface_overrides "$batch"
    if ! nft -f "$batch"; then
        rm -f "$batch"
        echo "Fast nft switch failed; falling back to full pbr restart" >&2
        /etc/init.d/pbr restart
    fi
    rm -f "$batch"
else
    echo "Cached PBR lists are missing; falling back to full pbr restart" >&2
    /etc/init.d/pbr restart
fi

flush_client_conntrack
grep -n "^TARGET_INTERFACE" "$PBR_SCRIPT"
echo "OpenCCK/AntiFilter list switched to $TARGET_INTERFACE"
EOF

cat > /usr/bin/vpn1 <<'EOF'
#!/bin/sh
exec /usr/bin/vpn-switch awg1
EOF

cat > /usr/bin/vpn2 <<'EOF'
#!/bin/sh
exec /usr/bin/vpn-switch awg2
EOF

cat > /usr/bin/vpn3 <<'EOF'
#!/bin/sh
exec /usr/bin/vpn-switch lokvpn
EOF

cat > /usr/bin/vpn4 <<'EOF'
#!/bin/sh
exec /usr/bin/vpn-switch vpntype
EOF

cat > /usr/bin/vpn-lokvpn <<'EOF'
#!/bin/sh
exec /usr/bin/vpn-switch lokvpn
EOF

cat > /usr/bin/vpn-vpntype <<'EOF'
#!/bin/sh
exec /usr/bin/vpn-switch vpntype
EOF

chmod +x /usr/bin/vpn-switch /usr/bin/vpn1 /usr/bin/vpn2 /usr/bin/vpn3 /usr/bin/vpn4 /usr/bin/vpn-lokvpn /usr/bin/vpn-vpntype

cat > "$CHECK_SCRIPT" <<'EOF'
#!/bin/sh

PBR_SCRIPT="${PBR_SCRIPT:-/usr/share/pbr/pbr.user.opencck-merged-vpn}"
TARGET_TABLE="${TARGET_TABLE:-inet fw4}"
DEFAULT_CHANNELS="${DEFAULT_CHANNELS:-awg1 awg2 lokvpn vpntype}"

is_safe_iface() {
    case "$1" in
        ''|*[!A-Za-z0-9_.-]*) return 1 ;;
        *) return 0 ;;
    esac
}

configured_channels() {
    {
        printf '%s\n' $DEFAULT_CHANNELS
        if command -v uci >/dev/null 2>&1; then
            uci -q get pbr.config.supported_interface 2>/dev/null | tr ' ' '\n' || true
        fi
    } | while read -r iface; do
        is_safe_iface "$iface" && printf '%s\n' "$iface"
    done | awk '!seen[$0]++'
}

echo "== target interface =="
grep -n "^TARGET_INTERFACE" "$PBR_SCRIPT" 2>/dev/null || echo "TARGET_INTERFACE not found"

echo
echo "== pbr status summary =="
/etc/init.d/pbr status 2>/dev/null | grep -E "started|monitoring|Running /usr/share/pbr|pbr_|default via|Forwarding is enabled" || true

echo
echo "== nft set entry counts =="
for iface in $(configured_channels); do
    set_name="pbr_${iface}_4_dst_ip_user"
    nft list set $TARGET_TABLE "$set_name" >/dev/null 2>&1 || continue
    count="$(nft list set $TARGET_TABLE "$set_name" 2>/dev/null \
        | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?' \
        | wc -l)"
    echo "$set_name: $count"
done

echo
echo "== tunnel status =="
for iface in $(configured_channels); do
    echo "-- $iface --"
    ip -4 addr show dev "$iface" 2>/dev/null | sed 's/^/  /' || echo "  no kernel interface"
    ip -4 route show dev "$iface" 2>/dev/null | sed 's/^/  route: /' || true
    if command -v awg >/dev/null 2>&1 && awg show "$iface" >/dev/null 2>&1; then
        awg show "$iface" 2>/dev/null | sed 's/^/  /'
    elif command -v wg >/dev/null 2>&1 && wg show "$iface" >/dev/null 2>&1; then
        wg show "$iface" 2>/dev/null | sed 's/^/  /'
    fi
done
EOF

chmod +x "$CHECK_SCRIPT"

echo "Installed: /usr/bin/vpn1"
echo "Installed: /usr/bin/vpn2"
echo "Installed: /usr/bin/vpn3"
echo "Installed: /usr/bin/vpn4"
echo "Installed: /usr/bin/vpn-lokvpn"
echo "Installed: /usr/bin/vpn-vpntype"
echo "Installed: $CHECK_SCRIPT"
echo
echo "Run:"
echo "  vpn1"
echo "  $CHECK_SCRIPT"
echo "  vpn2"
echo "  $CHECK_SCRIPT"
echo "  vpn-lokvpn"
echo "  vpn-vpntype"
