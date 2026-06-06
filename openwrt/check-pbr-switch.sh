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
