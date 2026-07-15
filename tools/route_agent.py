#!/usr/bin/env python3
"""Managed route agent prototype.

The agent fetches desired state from the control server, resolves domains,
checks current local routes, and can apply host routes on Linux and Windows.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "data" / "route_agent_cache.json"
DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
AGENT_VERSION = "0.1"
DEFAULT_HTTP_TIMEOUT = float(os.environ.get("VPN_AGENT_HTTP_TIMEOUT", "60"))
GEO_BLOCK_PATTERNS = [
    "gemini isn't currently supported in your country",
    "gemini isn\u2019t currently supported in your country",
    "isn't currently supported in your country",
    "isn\u2019t currently supported in your country",
    "not currently supported in your country",
    "not available in your country",
    "services are not available in your country",
    "country is not supported",
    "unsupported country",
]
PROBE_BODY_LIMIT_BYTES = 512 * 1024


@dataclass(frozen=True)
class LocalRoute:
    destination: str
    route: str | None
    error: str | None = None


def request_json(url: str, *, token: str, method: str = "GET", data: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": f"cudy-route-agent/{AGENT_VERSION}",
    }
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=DEFAULT_HTTP_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    return json.loads(raw)


def api_url(server_url: str, path: str) -> str:
    return f"{server_url.rstrip('/')}/{path.lstrip('/')}"


def parse_server_urls(value: str) -> list[str]:
    result: list[str] = []
    for item in re_split_urls(value):
        if item and item not in result:
            result.append(item)
    return result


def manifest_url_candidates() -> list[str]:
    return parse_server_urls(os.environ.get("VPN_CONTROL_ENDPOINT_MANIFEST_URLS", ""))


def discover_control_urls_from_manifests() -> list[str]:
    discovered: list[str] = []
    for manifest_url in manifest_url_candidates():
        try:
            request = Request(manifest_url, headers={"User-Agent": f"cudy-route-agent/{AGENT_VERSION}"})
            with urlopen(request, timeout=min(DEFAULT_HTTP_TIMEOUT, 10)) as response:
                raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            endpoints = payload.get("endpoints") if isinstance(payload, dict) else None
            if not isinstance(endpoints, list):
                continue
            ordered = sorted(
                (item for item in endpoints if isinstance(item, dict)),
                key=lambda item: int(item.get("priority") or 999),
            )
            for item in ordered:
                url = str(item.get("url") or "").strip()
                if url and url not in discovered:
                    discovered.append(url)
        except Exception:
            continue
    return discovered


def re_split_urls(value: str) -> list[str]:
    # URLs do not contain spaces in our config; commas/semicolons are accepted
    # so PowerShell env files stay easy to edit.
    items: list[str] = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part:
            items.append(part)
    return items


def server_url_candidates(args: argparse.Namespace) -> list[str]:
    raw = getattr(args, "server_urls", "") or os.environ.get("VPN_CONTROL_URLS", "")
    urls = parse_server_urls(raw)
    primary = (getattr(args, "server_url", "") or "").strip()
    if primary and primary not in urls:
        urls.insert(0, primary)
    for discovered in discover_control_urls_from_manifests():
        if discovered not in urls:
            urls.append(discovered)
    return urls or [DEFAULT_SERVER_URL]


def request_json_failover(
    args: argparse.Namespace,
    path: str,
    *,
    token: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> Any:
    errors: list[str] = []
    for server_url in server_url_candidates(args):
        try:
            payload = request_json(api_url(server_url, path), token=token, method=method, data=data)
            args.server_url = server_url
            os.environ["VPN_CONTROL_ACTIVE_URL"] = server_url
            return payload
        except Exception as exc:
            errors.append(f"{server_url}: {exc}")
    raise RuntimeError("All control URLs failed: " + " | ".join(errors))


def load_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("VPN_AGENT_TOKEN") or ""
    if not token:
        raise ValueError("Agent token is required. Pass --token or set VPN_AGENT_TOKEN.")
    return token.strip()


def fetch_config(args: argparse.Namespace) -> dict[str, Any]:
    token = load_token(args)
    config = request_json_failover(args, "/api/agent/config", token=token)
    if args.cache:
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        args.cache.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config


def fetch_probe_jobs(args: argparse.Namespace) -> list[dict[str, Any]]:
    token = load_token(args)
    payload = request_json_failover(args, f"/api/agent/probe-jobs?limit={int(args.limit)}", token=token)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise RuntimeError("Control server returned invalid probe job payload")
    return jobs


def post_probe_job_result(args: argparse.Namespace, *, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    token = load_token(args)
    payload = request_json_failover(
        args,
        "/api/agent/probe-jobs/result",
        token=token,
        method="POST",
        data={"job_id": job_id, "result": result},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Control server returned invalid probe job result payload")
    return payload


def load_cached_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Cached config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_ipv4(domain: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    try:
        answers = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    for answer in answers:
        ip = answer[4][0]
        if ip not in seen:
            result.append(ip)
            seen.add(ip)
    return result


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if is_windows():
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_text(command: list[str], *, timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.Popen(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            try:
                output, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                output = ""
            return 1, f"Command timed out after {timeout}s: {shlex.join(command)}\n{(output or '').strip()}"
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode or 0, (output or "").strip()


def current_platform() -> str:
    return platform.system().lower()


def is_linux() -> bool:
    return current_platform() == "linux"


def is_windows() -> bool:
    return current_platform() == "windows"


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_powershell(script: str, *, timeout: int = 10) -> tuple[int, str]:
    utf8_prefix = (
        "$utf8 = [System.Text.UTF8Encoding]::new($false); "
        "[Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8; "
    )
    return run_text(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", utf8_prefix + script],
        timeout=timeout,
    )


def run_powershell_file(script: str, *, timeout: int = 120) -> tuple[int, str]:
    utf8_prefix = (
        "$utf8 = [System.Text.UTF8Encoding]::new($false); "
        "[Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8; "
    )
    handle, raw_path = tempfile.mkstemp(prefix="cudy-route-batch-", suffix=".ps1")
    path = Path(raw_path)
    try:
        os.close(handle)
        path.write_text(utf8_prefix + script, encoding="utf-8-sig")
        return run_text(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            timeout=timeout,
        )
    finally:
        path.unlink(missing_ok=True)


def powershell_json(script: str) -> Any:
    rc, output = run_powershell(script)
    if rc != 0:
        raise RuntimeError(output or f"PowerShell failed rc={rc}")
    if not output:
        return None
    return json.loads(output)


def listify(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def linux_route_get(ip: str) -> LocalRoute:
    rc, output = run_text(["ip", "-4", "route", "get", ip])
    if rc != 0:
        return LocalRoute(destination=ip, route=None, error=output or f"ip route get failed rc={rc}")
    return LocalRoute(destination=ip, route=output)


def windows_route_get(ip: str) -> LocalRoute:
    script = f"""
$route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix {ps_quote(ip + '/32')} -ErrorAction SilentlyContinue |
  Sort-Object RouteMetric, InterfaceMetric |
  Select-Object -First 1 DestinationPrefix,InterfaceIndex,InterfaceAlias,NextHop,RouteMetric,InterfaceMetric
if ($null -eq $route) {{
  $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric, InterfaceMetric |
    Select-Object -First 1 DestinationPrefix,InterfaceIndex,InterfaceAlias,NextHop,RouteMetric,InterfaceMetric
}}
if ($null -eq $route) {{
  exit 2
}}
$adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
[pscustomobject]@{{
  DestinationPrefix = $route.DestinationPrefix
  InterfaceIndex = $route.InterfaceIndex
  InterfaceAlias = if ($null -ne $adapter) {{ $adapter.Name }} else {{ $route.InterfaceAlias }}
  NextHop = $route.NextHop
  RouteMetric = $route.RouteMetric
  InterfaceMetric = $route.InterfaceMetric
}} | ConvertTo-Json -Compress
"""
    rc, output = run_powershell(script)
    if rc != 0:
        return LocalRoute(destination=ip, route=None, error=output or f"Find-NetRoute failed rc={rc}")
    try:
        item = json.loads(output)
    except json.JSONDecodeError:
        return LocalRoute(destination=ip, route=output)
    route = (
        f"{item.get('DestinationPrefix')} via {item.get('NextHop')} "
        f"ifIndex={item.get('InterfaceIndex')} alias={item.get('InterfaceAlias')} "
        f"metric={item.get('RouteMetric')}/{item.get('InterfaceMetric')}"
    )
    return LocalRoute(destination=ip, route=route)


def default_linux_route() -> dict[str, str | None]:
    rc, output = run_text(["ip", "-4", "route", "show", "default"])
    if rc != 0 or not output:
        return {"dev": None, "via": None, "raw": output or None}
    candidates = output.splitlines()
    vpn_like = tuple(local_vpn_candidates())
    first = candidates[0]
    for line in candidates:
        parts = line.split()
        dev = parts[parts.index("dev") + 1] if "dev" in parts and parts.index("dev") + 1 < len(parts) else ""
        if dev and dev not in vpn_like:
            first = line
            break
    parts = first.split()
    dev = parts[parts.index("dev") + 1] if "dev" in parts and parts.index("dev") + 1 < len(parts) else None
    via = parts[parts.index("via") + 1] if "via" in parts and parts.index("via") + 1 < len(parts) else None
    return {"dev": dev, "via": via, "raw": first}


def default_windows_route() -> dict[str, str | None]:
    script = r"""
$vpnPattern = '(?i)(amn|amnezia|wireguard|wintun|openvpn|tap|tun|wg)'
$routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
  Sort-Object RouteMetric, InterfaceMetric
$selected = $null
foreach ($route in $routes) {
  $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
  if ($null -eq $adapter -or $adapter.Status -ne 'Up') {
    continue
  }
  $text = "$($adapter.Name) $($adapter.InterfaceDescription)"
  if ($text -notmatch $vpnPattern) {
    $addr = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '169.254.*' } |
      Select-Object -First 1 IPAddress,PrefixLength
    $selected = [pscustomobject]@{
      InterfaceAlias = $adapter.Name
      InterfaceIndex = $route.InterfaceIndex
      NextHop = $route.NextHop
      IPAddress = if ($null -ne $addr) { $addr.IPAddress } else { $null }
      PrefixLength = if ($null -ne $addr) { $addr.PrefixLength } else { $null }
      Raw = "$($route.DestinationPrefix) via $($route.NextHop) ifIndex=$($route.InterfaceIndex) alias=$($adapter.Name) metric=$($route.RouteMetric)/$($route.InterfaceMetric)"
    }
    break
  }
}
if ($null -eq $selected) {
  $route = $routes | Select-Object -First 1
  if ($null -ne $route) {
    $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
    $addr = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '169.254.*' } |
      Select-Object -First 1 IPAddress,PrefixLength
    $selected = [pscustomobject]@{
      InterfaceAlias = if ($null -ne $adapter) { $adapter.Name } else { $route.InterfaceAlias }
      InterfaceIndex = $route.InterfaceIndex
      NextHop = $route.NextHop
      IPAddress = if ($null -ne $addr) { $addr.IPAddress } else { $null }
      PrefixLength = if ($null -ne $addr) { $addr.PrefixLength } else { $null }
      Raw = "$($route.DestinationPrefix) via $($route.NextHop) ifIndex=$($route.InterfaceIndex) alias=$($route.InterfaceAlias) metric=$($route.RouteMetric)/$($route.InterfaceMetric)"
    }
  }
}
$selected | ConvertTo-Json -Compress
"""
    try:
        item = powershell_json(script)
    except Exception as exc:
        return {"dev": None, "via": None, "raw": str(exc)}
    if not item:
        return {"dev": None, "via": None, "raw": None}
    return {
        "dev": str(item.get("InterfaceAlias") or item.get("InterfaceIndex") or ""),
        "via": str(item.get("NextHop") or "") or None,
        "raw": str(item.get("Raw") or ""),
        "interface_index": str(item.get("InterfaceIndex") or ""),
        "interface_alias": str(item.get("InterfaceAlias") or ""),
        "ip_address": str(item.get("IPAddress") or ""),
        "prefix_length": str(item.get("PrefixLength") or ""),
    }


def linux_route_table() -> str | None:
    if not is_linux():
        return None
    rc, output = run_text(["ip", "-4", "route", "show"])
    return output if rc == 0 else None


def windows_route_table() -> str | None:
    script = r"""
Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Sort-Object DestinationPrefix,RouteMetric,InterfaceMetric |
  Select-Object -First 120 DestinationPrefix,InterfaceIndex,InterfaceAlias,NextHop,RouteMetric,InterfaceMetric |
  Format-Table -AutoSize | Out-String -Width 220
"""
    rc, output = run_powershell(script)
    return output if rc == 0 else None


def local_vpn_candidates() -> list[str]:
    if is_linux():
        rc, output = run_text(["ip", "-o", "link", "show"])
        if rc != 0:
            return []
        candidates: list[str] = []
        prefixes = ("amn", "wg", "awg", "tun", "ppp")
        for line in output.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            name = parts[1].strip().split("@", 1)[0]
            if name.startswith(prefixes):
                candidates.append(name)
        return candidates
    if is_windows():
        script = r"""
$pattern = '(?i)(amn|amnezia|wireguard|wintun|openvpn|tap|tun|wg)'
Get-NetAdapter -ErrorAction SilentlyContinue |
  Where-Object { $_.Status -eq 'Up' -and ("$($_.Name) $($_.InterfaceDescription)" -match $pattern) } |
  Select-Object Name,InterfaceIndex,InterfaceDescription |
  ConvertTo-Json -Compress
"""
        try:
            items = listify(powershell_json(script))
        except Exception:
            return []
        candidates: list[str] = []
        for item in items:
            name = str(item.get("Name") or "")
            if name:
                candidates.append(name)
        return candidates
    return []


def parse_interface_map(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--interface-map must look like server_id=iface: {item}")
        server_id, iface = item.split("=", 1)
        server_id = server_id.strip()
        iface = iface.strip()
        if not server_id or not iface:
            raise ValueError(f"--interface-map must look like server_id=iface: {item}")
        result[server_id] = iface
    return result


def parse_csv_items(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def windows_interface_ipv4(interface_alias: str) -> str | None:
    script = f"""
$addr = Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias {ps_quote(interface_alias)} -ErrorAction SilentlyContinue |
  Where-Object {{ $_.IPAddress -notlike '169.254.*' }} |
  Select-Object -First 1 IPAddress
if ($null -ne $addr) {{ $addr.IPAddress }}
"""
    rc, output = run_powershell(script)
    if rc != 0:
        return None
    value = output.strip()
    return value or None


def probe_route_prefixes(url: str) -> list[str]:
    parsed_url = urlparse(url)
    host = parsed_url.hostname
    if not host:
        return []
    try:
        address = ipaddress.ip_address(host)
        return [f"{address}/32"] if address.version == 4 else []
    except ValueError:
        return [f"{ip}/32" for ip in resolve_ipv4(host)]


def windows_set_probe_routes(prefixes: list[str], *, interface_name: str, add: bool) -> None:
    if not prefixes:
        return
    prefixes_script = "@(" + ",".join(ps_quote(item) for item in prefixes) + ")"
    interface_arg = (
        "-InterfaceIndex " + str(int(interface_name))
        if interface_name.isdigit()
        else "-InterfaceAlias " + ps_quote(interface_name)
    )
    if add:
        script = (
            f"$prefixes = {prefixes_script}; "
            "$prefixes | ForEach-Object { "
            "$dest = $_; "
            f"Remove-NetRoute -DestinationPrefix $dest {interface_arg} -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue; "
            f"New-NetRoute -DestinationPrefix $dest {interface_arg} -NextHop '0.0.0.0' "
            "-RouteMetric 0 -PolicyStore ActiveStore -ErrorAction Stop "
            "}"
        )
    else:
        script = (
            f"$prefixes = {prefixes_script}; "
            "$prefixes | ForEach-Object { "
            "$dest = $_; "
            f"Remove-NetRoute -DestinationPrefix $dest {interface_arg} -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue "
            "}"
        )
    rc, output = run_powershell(script, timeout=15)
    if rc != 0 and add:
        raise RuntimeError(output or f"failed to add temporary probe route for {interface_name}")


def probe_bind_value(interface_name: str) -> str:
    if is_windows():
        return windows_interface_ipv4(interface_name) or ""
    return interface_name


def http_probe_reachable(http_code: int) -> bool:
    # For Auto routing we need to know whether the target is reachable through
    # the exit. A redirect or auth/client-error page still proves the route.
    return 200 <= http_code < 500


def body_geo_block_evidence(body_text: str) -> str:
    normalized = body_text.lower().replace("\u2019", "'")
    for pattern in GEO_BLOCK_PATTERNS:
        normalized_pattern = pattern.lower().replace("\u2019", "'")
        index = normalized.find(normalized_pattern)
        if index < 0:
            continue
        start = max(0, index - 80)
        end = min(len(body_text), index + len(pattern) + 120)
        return " ".join(body_text[start:end].split())[:260]
    return ""


def apply_semantic_probe_check(
    parsed: dict[str, Any],
    *,
    url: str,
    body_text: str,
    success_pattern: str = "",
    failure_pattern: str = "",
) -> None:
    evidence = body_geo_block_evidence(body_text)
    if evidence:
        parsed["semantic_status"] = "geo_blocked"
        parsed["semantic_evidence"] = evidence
        parsed["ok"] = False
    elif failure_pattern and re.search(failure_pattern, body_text, re.IGNORECASE | re.MULTILINE):
        parsed["semantic_status"] = "failure_pattern"
        parsed["ok"] = False
    elif success_pattern and not re.search(success_pattern, body_text, re.IGNORECASE | re.MULTILINE):
        parsed["semantic_status"] = "success_pattern_missing"
        parsed["ok"] = False
    else:
        parsed["semantic_status"] = "ok"


def curl_probe(
    *,
    url: str,
    interface_name: str,
    connect_timeout: int,
    max_time: int,
    success_pattern: str = "",
    failure_pattern: str = "",
) -> dict[str, Any]:
    bind_value = probe_bind_value(interface_name)
    body_file = tempfile.NamedTemporaryFile(prefix="cudy-probe-", suffix=".body", delete=False)
    body_path = Path(body_file.name)
    body_file.close()
    command = [
        "curl",
        "-4",
        "-L",
        "-sS",
        "-o",
        str(body_path),
        "--connect-timeout",
        str(connect_timeout),
        "--max-time",
        str(max_time),
        "-w",
        (
            "http_code=%{http_code}\n"
            "time_total=%{time_total}\n"
            "remote_ip=%{remote_ip}\n"
            "size_download=%{size_download}\n"
            "speed_download=%{speed_download}\n"
        ),
        url,
    ]
    if bind_value:
        command[6:6] = ["--interface", bind_value]
    started = time.time()
    try:
        rc, output = run_text(command, timeout=max_time + 5)
        body_bytes = body_path.read_bytes()[:PROBE_BODY_LIMIT_BYTES] if body_path.exists() else b""
    finally:
        try:
            body_path.unlink()
        except OSError:
            pass
    elapsed_ms = int((time.time() - started) * 1000)
    parsed: dict[str, Any] = {
        "rc": rc,
        "elapsed_ms": elapsed_ms,
        "interface": interface_name,
        "bind": bind_value,
        "raw": output,
    }
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    try:
        parsed["http_code_int"] = int(parsed.get("http_code") or 0)
    except ValueError:
        parsed["http_code_int"] = 0
    try:
        parsed["time_total_ms"] = int(float(parsed.get("time_total") or "0") * 1000)
    except ValueError:
        parsed["time_total_ms"] = None
    try:
        parsed["speed_mbps"] = round(float(parsed.get("speed_download") or "0") * 8 / 1_000_000, 2)
    except ValueError:
        parsed["speed_mbps"] = None
    parsed["ok"] = http_probe_reachable(int(parsed["http_code_int"]))
    body_text = body_bytes.decode("utf-8", errors="replace")
    apply_semantic_probe_check(
        parsed,
        url=url,
        body_text=body_text,
        success_pattern=success_pattern,
        failure_pattern=failure_pattern,
    )
    return parsed


def tcp_probe(
    *,
    url: str,
    interface_name: str,
    connect_timeout: int,
    max_time: int,
) -> dict[str, Any]:
    parsed_url = urlparse(url)
    if parsed_url.scheme != "tcp" or not parsed_url.hostname or parsed_url.port is None:
        raise ValueError(f"TCP probe URL must look like tcp://host:port: {url}")

    bind_value = probe_bind_value(interface_name)
    started = time.time()
    result: dict[str, Any] = {
        "probe_type": "tcp",
        "interface": interface_name,
        "bind": bind_value,
        "remote_ip": parsed_url.hostname,
        "remote_port": parsed_url.port,
        "ok": False,
    }
    timeout = max(1, min(connect_timeout, max_time))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if bind_value and not is_windows():
                sock.bind((bind_value, 0))
            sock.connect((parsed_url.hostname, parsed_url.port))
            result["rc"] = 0
            result["ok"] = True
    except OSError as exc:
        result["rc"] = getattr(exc, "errno", None) or 1
        result["error"] = str(exc)
    finally:
        elapsed_ms = int((time.time() - started) * 1000)
        result["elapsed_ms"] = elapsed_ms
        result["time_total_ms"] = elapsed_ms
    return result


def run_single_probe(
    *,
    url: str,
    interface_name: str,
    connect_timeout: int,
    max_time: int,
    success_pattern: str = "",
    failure_pattern: str = "",
) -> dict[str, Any]:
    prefixes = probe_route_prefixes(url) if is_windows() else []
    routes_added = False
    try:
        if prefixes:
            windows_set_probe_routes(prefixes, interface_name=interface_name, add=True)
            routes_added = True
        if url.lower().startswith("tcp://"):
            return tcp_probe(
                url=url,
                interface_name=interface_name,
                connect_timeout=connect_timeout,
                max_time=max_time,
            )
        return curl_probe(
            url=url,
            interface_name=interface_name,
            connect_timeout=connect_timeout,
            max_time=max_time,
            success_pattern=success_pattern,
            failure_pattern=failure_pattern,
        )
    finally:
        if routes_added:
            windows_set_probe_routes(prefixes, interface_name=interface_name, add=False)


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    interface_map = parse_interface_map(args.interface_map)
    candidates = parse_csv_items(args.candidates)
    if not candidates:
        candidates = list(interface_map)
    if not candidates:
        raise ValueError("probe requires --candidates or at least one --interface-map")
    domain = str(args.domain or "").strip().lower()
    url = args.url or f"https://{domain}/"
    checks: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    for index, server_id in enumerate(candidates, start=1):
        iface = interface_map.get(server_id)
        check: dict[str, Any] = {
            "server_id": server_id,
            "index": index,
            "interface": iface,
            "ok": False,
        }
        if not iface:
            check["status"] = "no_interface"
            checks.append(check)
            continue
        probe = run_single_probe(
            url=url,
            interface_name=iface,
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
            success_pattern=str(getattr(args, "success_pattern", "") or ""),
            failure_pattern=str(getattr(args, "failure_pattern", "") or ""),
        )
        check.update(probe)
        check["status"] = "ok" if probe.get("ok") else str(probe.get("semantic_status") or "failed")
        checks.append(check)
        if check["ok"] and (winner is None or (check.get("time_total_ms") or 10**9) < (winner.get("time_total_ms") or 10**9)):
            winner = check
    return {
        "schema_version": 1,
        "agent_version": AGENT_VERSION,
        "platform": current_platform(),
        "domain": domain,
        "url": url,
        "candidate_server_ids": candidates,
        "winner": winner,
        "checks": checks,
        "ok": bool(winner),
    }


def run_probe_jobs(args: argparse.Namespace) -> dict[str, Any]:
    interface_map = parse_interface_map(args.interface_map)
    jobs = fetch_probe_jobs(args)
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("id") or "")
        try:
            probe_args = argparse.Namespace(
                domain=str(job.get("domain") or ""),
                url=job.get("url") or None,
                candidates=",".join(job.get("candidate_server_ids") or []),
                interface_map=args.interface_map,
                connect_timeout=int(job.get("connect_timeout") or args.connect_timeout),
                max_time=int(job.get("max_time") or args.max_time),
                success_pattern=str(job.get("success_pattern") or ""),
                failure_pattern=str(job.get("failure_pattern") or ""),
            )
            result = run_probe(probe_args)
            posted = post_probe_job_result(args, job_id=job_id, result=result)
            completed.append(
                {
                    "job_id": job_id,
                    "domain": job.get("domain"),
                    "ok": bool(result.get("ok")),
                    "winner": (result.get("winner") or {}).get("server_id") if isinstance(result.get("winner"), dict) else None,
                    "posted": posted,
                }
            )
        except Exception as exc:
            failed.append({"job_id": job_id, "domain": job.get("domain"), "error": str(exc)})
    return {
        "schema_version": 1,
        "agent_version": AGENT_VERSION,
        "platform": current_platform(),
        "ok": not failed,
        "jobs": len(jobs),
        "completed": completed,
        "failed": failed,
        "interface_map": interface_map,
    }


def route_command_for_ip(
    *,
    ip: str,
    server_id: str,
    interface_map: dict[str, str],
    default_route: dict[str, str | None],
) -> tuple[str, str]:
    if is_windows():
        return windows_route_command_for_ip(
            ip=ip,
            server_id=server_id,
            interface_map=interface_map,
            default_route=default_route,
        )
    if server_id in {"", "auto", "direct"}:
        dev = default_route.get("dev")
        via = default_route.get("via")
        if dev and via:
            return "direct", f"ip route replace {ip}/32 via {via} dev {dev}"
        if dev:
            return "direct", f"ip route replace {ip}/32 dev {dev}"
        return "direct", "# cannot build direct route: default route not detected"
    iface = interface_map.get(server_id)
    if iface:
        return "tunnel", f"ip route replace {ip}/32 dev {iface}"
    return "tunnel", f"# map server '{server_id}' to a local VPN interface with --interface-map {server_id}=IFACE"


def windows_replace_route_command(
    destination_prefix: str,
    *,
    interface: str,
    next_hop: str | None = None,
    route_metric: int = 1,
    interface_metric: int | None = None,
) -> str:
    interface_arg = "-InterfaceIndex " + str(int(interface)) if interface.isdigit() else "-InterfaceAlias " + ps_quote(interface)
    hop = next_hop or "0.0.0.0"
    metric_script = ""
    if interface_metric is not None:
        metric_script = f"Set-NetIPInterface {interface_arg} -AddressFamily IPv4 -InterfaceMetric {int(interface_metric)} -ErrorAction Stop; "
    script = (
        f"$dest = {ps_quote(destination_prefix)}; "
        f"{metric_script}"
        "Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $dest -PolicyStore ActiveStore -ErrorAction SilentlyContinue | "
        "Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue; "
        f"New-NetRoute -DestinationPrefix $dest {interface_arg} -NextHop {ps_quote(hop)} "
        f"-RouteMetric {int(route_metric)} -PolicyStore ActiveStore -ErrorAction Stop"
    )
    return "powershell:" + script


def windows_remove_routes_command(destination_prefixes: list[str], *, interface: str) -> str:
    interface_filter = (
        f"$_.InterfaceIndex -eq {int(interface)}"
        if interface.isdigit()
        else f"$_.InterfaceAlias -eq {ps_quote(interface)}"
    )
    prefixes = "@(" + ",".join(ps_quote(item) for item in destination_prefixes) + ")"
    script = (
        f"$prefixes = {prefixes}; "
        "$prefixes | ForEach-Object { "
        "$dest = $_; "
        "Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $dest -PolicyStore ActiveStore -ErrorAction SilentlyContinue | "
        f"Where-Object {{ {interface_filter} }} | "
        "Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue "
        "}"
    )
    return "powershell:" + script


def cleanup_command_for_prefix(target: str, *, interface: str) -> str:
    if is_windows():
        return windows_remove_routes_command([target], interface=interface)
    return f"optional:ip route delete {target} dev {interface}"


def windows_route_command_for_ip(
    *,
    ip: str,
    server_id: str,
    interface_map: dict[str, str],
    default_route: dict[str, str | None],
) -> tuple[str, str]:
    if server_id in {"", "auto", "direct"}:
        interface = str(default_route.get("interface_index") or default_route.get("dev") or "")
        if not interface:
            return "direct", "# cannot build direct route: default route not detected"
        return "direct", windows_replace_route_command(f"{ip}/32", interface=interface, next_hop=default_route.get("via"))
    iface = interface_map.get(server_id)
    if iface:
        return "tunnel", windows_replace_route_command(f"{ip}/32", interface=iface, next_hop="0.0.0.0")
    return "tunnel", f"# map server '{server_id}' to a local VPN interface with --interface-map {server_id}=IFACE_OR_INDEX"


def build_plan(
    config: dict[str, Any],
    *,
    interface_map: dict[str, str],
    inspect_routes: bool = True,
) -> dict[str, Any]:
    if is_linux():
        default_route = default_linux_route()
        route_table = linux_route_table()
    elif is_windows():
        default_route = default_windows_route()
        route_table = windows_route_table() if inspect_routes else None
    else:
        default_route = {"dev": None, "via": None, "raw": None}
        route_table = None
    domains: list[dict[str, Any]] = []
    ip_routes: list[dict[str, Any]] = []
    cleanup_ip_routes: list[dict[str, Any]] = []
    warnings: list[str] = list(config.get("warnings") or [])
    local_candidates = local_vpn_candidates()

    for route in config.get("domain_routes", []):
        domain = str(route.get("domain") or "")
        server_id = str(route.get("server_id") or "")
        ips = resolve_ipv4(domain)
        planned_ips: list[dict[str, Any]] = []
        for ip in ips:
            if not inspect_routes:
                current = LocalRoute(ip, None, None)
            elif is_linux():
                current = linux_route_get(ip)
            elif is_windows():
                current = windows_route_get(ip)
            else:
                current = LocalRoute(ip, None, "route inspection is only supported on Linux and Windows")
            action, command = route_command_for_ip(
                ip=ip,
                server_id=server_id,
                interface_map=interface_map,
                default_route=default_route,
            )
            planned_ips.append(
                {
                    "ip": ip,
                    "action": action,
                    "command": command,
                    "current_route": current.route,
                    "current_error": current.error,
                }
            )
        domains.append(
            {
                "domain": domain,
                "source": route.get("source"),
                "requested_server_id": route.get("requested_server_id"),
                "server_id": server_id,
                "resolved_server_id": route.get("resolved_server_id"),
                "server_label": (route.get("server") or {}).get("label"),
                "remote_interface": (route.get("server") or {}).get("interface"),
                "ips": planned_ips,
                "resolve_ok": bool(ips),
            }
        )

    for route in config.get("ip_routes", []):
        target = str(route.get("target_cidr") or "")
        server_id = str(route.get("server_id") or "")
        ip = target.split("/", 1)[0]
        action, command = route_command_for_ip(
            ip=ip,
            server_id=server_id,
            interface_map=interface_map,
            default_route=default_route,
        )
        ip_routes.append(
            {
                "target_cidr": target,
                "source": route.get("source"),
                "requested_server_id": route.get("requested_server_id"),
                "server_id": server_id,
                "resolved_server_id": route.get("resolved_server_id"),
                "auto_cache_key": route.get("auto_cache_key"),
                "action": action,
                "command": command.replace(f"{ip}/32", target),
            }
        )

    cleanup_interfaces = sorted(
        {
            str(item)
            for item in [
                *interface_map.values(),
                *local_candidates,
            ]
            if str(item or "").strip()
        }
    )
    cleanup_targets: list[str] = []
    seen_cleanup_targets: set[str] = set()
    for route in config.get("cleanup_ip_routes", []):
        target = str(route.get("target_cidr") or "")
        if not target:
            continue
        if target not in seen_cleanup_targets:
            cleanup_targets.append(target)
            seen_cleanup_targets.add(target)
        cleanup_ip_routes.append(
            {
                "target_cidr": target,
                "source": route.get("source"),
                "requested_server_id": route.get("requested_server_id"),
                "auto_cache_key": route.get("auto_cache_key"),
                "commands": [],
            }
        )
    cleanup_commands: list[str] = []
    if cleanup_targets and cleanup_interfaces:
        if is_windows():
            cleanup_commands = [
                windows_remove_routes_command(cleanup_targets, interface=iface)
                for iface in cleanup_interfaces
            ]
        else:
            cleanup_commands = [
                cleanup_command_for_prefix(target, interface=iface)
                for target in cleanup_targets
                for iface in cleanup_interfaces
            ]

    return {
        "schema_version": 1,
        "agent_version": AGENT_VERSION,
        "generated_at": int(time.time()),
        "platform": current_platform(),
        "user": config.get("user"),
        "device": config.get("device"),
        "default_route": default_route,
        "route_table": route_table,
        "local_vpn_candidates": local_candidates,
        "interface_map": interface_map,
        "domain_routes": domains,
        "ip_routes": ip_routes,
        "cleanup_ip_routes": cleanup_ip_routes,
        "cleanup_commands": cleanup_commands,
        "warnings": warnings,
    }


def print_text_plan(plan: dict[str, Any]) -> None:
    user = plan.get("user") or {}
    device = plan.get("device") or {}
    print(f"user={user.get('id')} device={device.get('id')} platform={plan.get('platform')}")
    print(f"default_route={plan.get('default_route', {}).get('raw') or '-'}")
    candidates = plan.get("local_vpn_candidates") or []
    print(f"local_vpn_candidates={', '.join(candidates) if candidates else '-'}")
    route_table = plan.get("route_table") or ""
    full_tunnel_lines = [
        line for line in route_table.splitlines()
        if line.startswith("0.0.0.0/1 ") or line.startswith("128.0.0.0/1 ")
        or "0.0.0.0/1" in line or "128.0.0.0/1" in line
    ]
    if full_tunnel_lines:
        print("full_tunnel_routes=")
        for line in full_tunnel_lines:
            print(f"  {line}")
    if plan.get("warnings"):
        for warning in plan["warnings"]:
            print(f"WARNING: {warning}")
    for route in plan.get("domain_routes", []):
        print(
            f"\n{route['domain']} -> {route['server_id']} "
            f"({route.get('server_label') or '-'}, remote_if={route.get('remote_interface') or '-'})"
        )
        if not route["resolve_ok"]:
            print("  resolve failed")
        for item in route["ips"]:
            print(f"  {item['ip']} current={item.get('current_route') or item.get('current_error') or '-'}")
            print(f"    dry-run: {item['command']}")
    for route in plan.get("ip_routes", []):
        print(f"\n{route['target_cidr']} -> {route['server_id']}")
        print(f"  dry-run: {route['command']}")
    for route in plan.get("cleanup_ip_routes", []):
        print(f"\ncleanup {route['target_cidr']} ({route.get('requested_server_id') or '-'})")
        for command in route.get("commands") or []:
            print(f"  dry-run: {command}")
    if plan.get("cleanup_commands"):
        print("\ncleanup batch commands")
        for command in plan.get("cleanup_commands") or []:
            print(f"  dry-run: {command}")


def post_status(args: argparse.Namespace, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    token = load_token(args)
    mode = getattr(args, "status_mode", "dry-run")
    domain_status = []
    ip_route_status = []
    if plan:
        for route in plan.get("domain_routes", []):
            domain_status.append(
                {
                    "domain": route.get("domain"),
                    "source": route.get("source"),
                    "requested_server_id": route.get("requested_server_id"),
                    "server_id": route.get("server_id"),
                    "server_label": route.get("server_label"),
                    "remote_interface": route.get("remote_interface"),
                    "resolve_ok": route.get("resolve_ok"),
                    "ips": [item.get("ip") for item in route.get("ips", []) if item.get("ip")],
                }
            )
        for route in plan.get("ip_routes", []):
            ip_route_status.append(
                {
                    "target_cidr": route.get("target_cidr"),
                    "source": route.get("source"),
                    "requested_server_id": route.get("requested_server_id"),
                    "server_id": route.get("server_id"),
                    "resolved_server_id": route.get("resolved_server_id"),
                    "auto_cache_key": route.get("auto_cache_key"),
                    "action": route.get("action"),
                }
            )
        for route in plan.get("cleanup_ip_routes", []):
            ip_route_status.append(
                {
                    "target_cidr": route.get("target_cidr"),
                    "source": route.get("source"),
                    "requested_server_id": route.get("requested_server_id"),
                    "server_id": "",
                    "resolved_server_id": None,
                    "auto_cache_key": route.get("auto_cache_key"),
                    "action": "cleanup",
                }
            )
    payload = {
        "schema_version": 1,
        "platform": current_platform(),
        "agent_version": AGENT_VERSION,
        "vpn_interfaces": plan.get("local_vpn_candidates", []) if plan else local_vpn_candidates(),
        "routes": {
            "domain_count": len(plan.get("domain_routes", [])) if plan else None,
            "ip_route_count": len(plan.get("ip_routes", [])) if plan else None,
        },
        "domain_routes": domain_status,
        "ip_routes": ip_route_status,
        "health": {
            "ok": not bool(plan.get("apply_errors")) if plan else True,
            "mode": mode,
            "applied": len(plan.get("applied_commands", [])) if plan else 0,
        },
        "capabilities": {
            "can_probe": True,
            "can_route": True,
            "can_manage_transports": bool(getattr(args, "can_manage_transports", False) or is_windows()),
        },
        "errors": [],
    }
    if plan and plan.get("apply_errors"):
        payload["errors"] = plan["apply_errors"]
    return request_json_failover(args, "/api/agent/status", token=token, method="POST", data=payload)


def try_post_status(args: argparse.Namespace, plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        return post_status(args, plan)
    except Exception as exc:
        if plan is not None:
            plan.setdefault("status_errors", []).append(str(exc))
        return None


def plan_commands(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    commands: list[str] = []
    blockers: list[str] = []
    for route in plan.get("domain_routes", []):
        for item in route.get("ips", []):
            command = str(item.get("command") or "")
            if command.startswith("ip route replace ") or command.startswith("powershell:"):
                commands.append(command)
            elif command:
                blockers.append(f"{route.get('domain')}/{item.get('ip')}: {command}")
    for route in plan.get("ip_routes", []):
        command = str(route.get("command") or "")
        if command.startswith("ip route replace ") or command.startswith("powershell:"):
            commands.append(command)
        elif command:
            blockers.append(f"{route.get('target_cidr')}: {command}")
    for route in plan.get("cleanup_ip_routes", []):
        for command in route.get("commands") or []:
            command = str(command or "")
            if (
                command.startswith("optional:")
                or command.startswith("ip route delete ")
                or command.startswith("powershell:")
            ):
                commands.append(command)
            elif command:
                blockers.append(f"{route.get('target_cidr')}: {command}")
    for command in plan.get("cleanup_commands") or []:
        command = str(command or "")
        if (
            command.startswith("optional:")
            or command.startswith("ip route delete ")
            or command.startswith("powershell:")
        ):
            commands.append(command)
        elif command:
            blockers.append(f"cleanup batch: {command}")
    return commands, blockers


def direct_baseline_commands(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    default_route = plan.get("default_route") or {}
    via = default_route.get("via")
    dev = default_route.get("dev")
    if not dev:
        return [], ["cannot restore direct baseline: physical default interface not detected"]
    commands: list[str] = []
    if plan.get("platform") == "windows":
        cleanup_prefixes = ["0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1"]
        ip_address = str(default_route.get("ip_address") or "")
        prefix_length = str(default_route.get("prefix_length") or "")
        if ip_address and prefix_length:
            try:
                network = ipaddress.ip_interface(f"{ip_address}/{prefix_length}").network
                cleanup_prefixes.append(str(network))
                cleanup_prefixes.append(f"{ip_address}/32")
                cleanup_prefixes.append(f"{network.network_address + 1}/32")
                cleanup_prefixes.append(f"{network.broadcast_address}/32")
            except ValueError:
                pass
        via_str = str(via or "")
        if via_str:
            cleanup_prefixes.append(f"{via_str}/32")
        seen: set[str] = set()
        cleanup_prefixes = [item for item in cleanup_prefixes if not (item in seen or seen.add(item))]
        vpn_interfaces = sorted({str(value) for value in (plan.get("interface_map") or {}).values() if value})
        cleanup_commands: list[str] = []
        for vpn_interface in vpn_interfaces:
            cleanup_commands.append(windows_remove_routes_command(cleanup_prefixes, interface=vpn_interface))
        commands.extend(cleanup_commands)
    for cidr in ("0.0.0.0/1", "128.0.0.0/1"):
        if plan.get("platform") == "windows":
            interface = str(default_route.get("interface_index") or dev)
            commands.append(
                windows_replace_route_command(
                    cidr,
                    interface=interface,
                    next_hop=via,
                    route_metric=0,
                    interface_metric=1,
                )
            )
        elif via:
            commands.append(f"ip route replace {cidr} via {via} dev {dev}")
        else:
            commands.append(f"ip route replace {cidr} dev {dev}")
    if plan.get("platform") == "windows":
        commands.extend(cleanup_commands)
    return commands, []


def run_route_command(command: str) -> tuple[bool, str]:
    if command.startswith("optional:"):
        ok, output = run_route_command(command.removeprefix("optional:"))
        return True, output if ok else f"ignored cleanup failure: {output}"
    if is_windows() and command.startswith("powershell:"):
        rc, output = run_powershell(command.removeprefix("powershell:"), timeout=45)
        return rc == 0, output
    argv = shlex.split(command)
    if is_linux() and hasattr(os, "geteuid") and os.geteuid() != 0:
        argv = ["sudo", "-n", *argv]
    rc, output = run_text(argv, timeout=30)
    return rc == 0, output


def run_windows_route_batch(commands: list[str]) -> list[dict[str, Any]]:
    entries: list[tuple[str, bool, str]] = []
    script_parts = ["$results = [System.Collections.Generic.List[object]]::new()"]
    for index, original in enumerate(commands):
        optional = original.startswith("optional:")
        normalized = original.removeprefix("optional:")
        if not normalized.startswith("powershell:"):
            raise RuntimeError(f"non-PowerShell command in Windows route batch: {original}")
        powershell_script = normalized.removeprefix("powershell:")
        entries.append((original, optional, powershell_script))
        script_parts.append(
            "$captured = ''; "
            "try { "
            f"$captured = (& {{ {powershell_script} }} 2>&1 | Out-String -Width 4096).Trim(); "
            f"$results.Add([pscustomobject]@{{Index={index};Ok=$true;Output=$captured}}) | Out-Null "
            "} catch { "
            "$captured = ($_ | Out-String -Width 4096).Trim(); "
            f"$results.Add([pscustomobject]@{{Index={index};Ok=$false;Output=$captured}}) | Out-Null "
            "}"
        )
    script_parts.append("$results | ConvertTo-Json -Compress -Depth 5")
    rc, output = run_powershell_file("; ".join(script_parts), timeout=120)
    if rc != 0:
        raise RuntimeError(output or f"PowerShell route batch failed rc={rc}")
    try:
        raw_results = listify(json.loads(output)) if output else []
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PowerShell route batch returned invalid JSON: {exc}: {output[-1000:]}") from exc
    result_by_index = {int(item.get("Index", -1)): item for item in raw_results if isinstance(item, dict)}
    applied: list[dict[str, Any]] = []
    for index, (original, optional, _script) in enumerate(entries):
        item = result_by_index.get(index)
        if item is None:
            applied.append({"command": original, "ok": optional, "output": "missing result from PowerShell route batch"})
            continue
        ok = bool(item.get("Ok"))
        output_text = str(item.get("Output") or "")
        if optional and not ok:
            ok = True
            output_text = f"ignored cleanup failure: {output_text}"
        applied.append({"command": original, "ok": ok, "output": output_text})
    return applied


def apply_plan(plan: dict[str, Any], *, yes: bool, direct_baseline: bool) -> dict[str, Any]:
    if not (is_linux() or is_windows()):
        raise RuntimeError("apply mode is currently Linux/Windows-only")
    if not yes:
        raise RuntimeError("apply mode requires --yes")
    commands, blockers = plan_commands(plan)
    if direct_baseline:
        baseline_commands, baseline_blockers = direct_baseline_commands(plan)
        commands = [*baseline_commands, *commands]
        blockers = [*baseline_blockers, *blockers]
    blockers = [blocker for blocker in blockers if str(blocker).strip()]
    if blockers:
        raise RuntimeError("cannot apply until all targets are mapped:\n" + "\n".join(blockers))
    if commands and is_windows() and all(command.removeprefix("optional:").startswith("powershell:") for command in commands):
        applied = run_windows_route_batch(commands)
    else:
        applied = []
        for command in commands:
            ok, output = run_route_command(command)
            applied.append({"command": command, "ok": ok, "output": output})
    errors = [f"{item['command']}: {item['output']}" for item in applied if not item["ok"]]
    plan["applied_commands"] = applied
    plan["apply_errors"] = errors
    plan["direct_baseline"] = bool(direct_baseline)
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Managed route-agent prototype.")
    parser.add_argument("--server-url", default=os.environ.get("VPN_CONTROL_URL", DEFAULT_SERVER_URL))
    parser.add_argument("--server-urls", default=os.environ.get("VPN_CONTROL_URLS", ""), help="Comma-separated failover control URLs. --server-url is still tried first.")
    parser.add_argument("--token", help="Device token. Defaults to VPN_AGENT_TOKEN.")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="Fetch agent config and print JSON.")
    config.add_argument("--cached", action="store_true", help="Use cached config instead of fetching.")
    config.add_argument("--json", action="store_true", help="Print JSON. Kept for symmetry; config output is always JSON.")

    plan = sub.add_parser("plan", help="Fetch config and print a dry-run route plan.")
    plan.add_argument("--cached", action="store_true", help="Use cached config instead of fetching.")
    plan.add_argument("--interface-map", action="append", default=[], help="Map server id to local interface, e.g. cudy=amn0.")
    plan.add_argument("--post-status", action="store_true", help="Post a dry-run health status after planning.")
    plan.add_argument("--can-manage-transports", action="store_true", help="Report that the calling platform wrapper can start transports from transport_plan.")
    plan.add_argument("--direct-baseline", action="store_true", help="Show commands that restore non-matched traffic to the physical default route.")
    plan.add_argument("--json", action="store_true")

    apply = sub.add_parser("apply", help="Apply route commands. Linux/Windows and requires --yes.")
    apply.add_argument("--cached", action="store_true", help="Use cached config instead of fetching.")
    apply.add_argument("--interface-map", action="append", default=[], help="Map server id to local interface, e.g. aktau=amn0.")
    apply.add_argument("--post-status", action="store_true", help="Post apply status after changing routes.")
    apply.add_argument("--can-manage-transports", action="store_true", help="Report that the calling platform wrapper can start transports from transport_plan.")
    apply.add_argument("--direct-baseline", action="store_true", help="Route non-matched IPv4 traffic through the physical default gateway using 0/1 and 128/1 routes.")
    apply.add_argument("--yes", action="store_true", help="Required confirmation for route changes.")
    apply.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Post a minimal agent status.")
    status.add_argument("--can-manage-transports", action="store_true", help="Report that the calling platform wrapper can start transports from transport_plan.")
    status.add_argument("--json", action="store_true")

    probe = sub.add_parser("probe", help="Probe a domain through candidate local interfaces.")
    probe.add_argument("domain", help="Domain to probe.")
    probe.add_argument("--url", help="Probe URL. Default is https://DOMAIN/.")
    probe.add_argument("--candidates", help="Comma-separated server ids in priority order.")
    probe.add_argument("--interface-map", action="append", default=[], help="Map server id to local interface, e.g. proxyde=proxyde.")
    probe.add_argument("--connect-timeout", type=int, default=5)
    probe.add_argument("--max-time", type=int, default=12)
    probe.add_argument("--success-pattern", default="", help="Optional response-body regex required for success.")
    probe.add_argument("--failure-pattern", default="", help="Optional response-body regex that rejects the candidate.")
    probe.add_argument("--json", action="store_true")

    probe_jobs = sub.add_parser("probe-jobs", help="Fetch and execute pending control-server probe jobs.")
    probe_jobs.add_argument("--interface-map", action="append", default=[], help="Map server id to local interface, e.g. proxyde=proxyde.")
    probe_jobs.add_argument("--limit", type=int, default=2)
    probe_jobs.add_argument("--connect-timeout", type=int, default=5, help="Fallback timeout when a job omits it.")
    probe_jobs.add_argument("--max-time", type=int, default=12, help="Fallback max time when a job omits it.")
    probe_jobs.add_argument("--json", action="store_true")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "config":
            config = load_cached_config(args.cache) if args.cached else fetch_config(args)
            print(json.dumps(config, ensure_ascii=False, indent=2))
            return 0
        if args.command == "plan":
            interface_map = parse_interface_map(args.interface_map)
            config = load_cached_config(args.cache) if args.cached else fetch_config(args)
            plan = build_plan(config, interface_map=interface_map)
            if args.direct_baseline:
                commands, blockers = direct_baseline_commands(plan)
                plan["direct_baseline_preview"] = {"commands": commands, "blockers": blockers}
            if args.post_status:
                args.status_mode = "dry-run"
                status = try_post_status(args, plan)
                plan["posted_status"] = status
            if args.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                print_text_plan(plan)
                if args.direct_baseline:
                    print("\nDirect baseline dry-run:")
                    for command in plan["direct_baseline_preview"]["commands"]:
                        print(f"  {command}")
                    for blocker in plan["direct_baseline_preview"]["blockers"]:
                        print(f"  {blocker}")
                if args.post_status:
                    print(f"\nstatus posted: {plan['posted_status']}")
                for item in plan.get("status_errors", []):
                    print(f"\nstatus post failed: {item}")
            return 0
        if args.command == "apply":
            interface_map = parse_interface_map(args.interface_map)
            config = load_cached_config(args.cache) if args.cached else fetch_config(args)
            plan = build_plan(config, interface_map=interface_map, inspect_routes=False)
            plan = apply_plan(plan, yes=args.yes, direct_baseline=args.direct_baseline)
            if args.post_status:
                args.status_mode = "apply"
                status = try_post_status(args, plan)
                plan["posted_status"] = status
            if args.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                print_text_plan(plan)
                print("\nApplied commands:")
                for item in plan.get("applied_commands", []):
                    status = "OK" if item.get("ok") else "FAIL"
                    print(f"  [{status}] {item.get('command')}")
                    if item.get("output"):
                        print(f"    {item['output']}")
                if plan.get("posted_status"):
                    print(f"\nstatus posted: {plan['posted_status']}")
                for item in plan.get("status_errors", []):
                    print(f"\nstatus post failed: {item}")
            return 0
        if args.command == "status":
            status = post_status(args)
            if args.json:
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                print(f"status posted: {status}")
            return 0
        if args.command == "probe":
            result = run_probe(args)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{result['domain']} -> {result['winner']['server_id'] if result['winner'] else '-'}")
                for check in result["checks"]:
                    print(
                        f"  {check['server_id']} iface={check.get('interface') or '-'} "
                        f"status={check.get('status')} http={check.get('http_code') or '-'} "
                        f"ms={check.get('time_total_ms') or check.get('elapsed_ms') or '-'} "
                        f"remote={check.get('remote_ip') or '-'}"
                    )
            return 0
        if args.command == "probe-jobs":
            result = run_probe_jobs(args)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"probe jobs: jobs={result['jobs']} completed={len(result['completed'])} failed={len(result['failed'])}")
                for item in result["completed"]:
                    print(f"  {item['job_id']} {item.get('domain') or '-'} winner={item.get('winner') or '-'} ok={item.get('ok')}")
                for item in result["failed"]:
                    print(f"  FAIL {item['job_id']} {item.get('domain') or '-'} {item.get('error')}")
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
