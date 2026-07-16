#!/usr/bin/env python3
"""Start the control server on a temporary DB and smoke-check public HTTP endpoints."""

from __future__ import annotations

import gzip
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "config" / "vpn_inventory.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_json(url: str, *, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def fetch_text(opener: urllib.request.OpenerDirector, url: str, *, timeout: int = 5) -> str:
    with opener.open(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def fetch_json_with_opener(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    timeout: int = 5,
    method: str = "GET",
) -> dict:
    if method == "GET":
        return json.loads(fetch_text(opener, url, timeout=timeout))
    request = urllib.request.Request(url, method=method)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(opener: urllib.request.OpenerDirector, url: str, payload: dict, *, timeout: int = 5) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_with_bearer(url: str, token: str, *, timeout: int = 5) -> dict:
    request = urllib.request.Request(url, headers={"authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json_with_bearer(url: str, token: str, payload: dict, *, timeout: int = 5) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json_public(url: str, payload: dict, *, timeout: int = 5) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def expect_http_error(callable_request, expected_status: int) -> None:
    try:
        callable_request()
    except urllib.error.HTTPError as exc:
        if exc.code != expected_status:
            raise AssertionError(f"expected HTTP {expected_status}, got HTTP {exc.code}") from exc
        return
    raise AssertionError(f"expected HTTP {expected_status}, request succeeded")


def wait_for_healthz(base_url: str, *, timeout_seconds: int = 20) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            payload = fetch_json(f"{base_url}/healthz", timeout=2)
            if payload.get("ok") is True:
                return
            last_error = RuntimeError(f"unexpected health payload: {payload!r}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
        time.sleep(0.3)
    raise RuntimeError(f"control server did not become healthy: {last_error}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-control-http-") as tmp:
        port = free_port()
        db_path = Path(tmp) / "vpn_control.db"
        base_url = f"http://127.0.0.1:{port}"
        command = [
            sys.executable,
            str(ROOT / "tools" / "vpn_control_app.py"),
            "--db",
            str(db_path),
            "--inventory",
            str(INVENTORY),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-auto-worker",
            "--no-provider-refresh-worker",
        ]
        subprocess.check_call(
            [
                sys.executable,
                str(ROOT / "tools" / "vpn_control_app.py"),
                "--db",
                str(db_path),
                "--inventory",
                str(INVENTORY),
                "create-user",
                "smoke-admin",
                "--role",
                "admin",
                "--password",
                "smoke-password",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
        )
        subprocess.check_call(
            [
                sys.executable,
                str(ROOT / "tools" / "vpn_control_app.py"),
                "--db",
                str(db_path),
                "--inventory",
                str(INVENTORY),
                "create-user",
                "other-user",
                "--role",
                "user",
                "--password",
                "other-password",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
        )
        subprocess.check_call(
            [
                sys.executable,
                str(ROOT / "tools" / "vpn_control_app.py"),
                "--db",
                str(db_path),
                "--inventory",
                str(INVENTORY),
                "create-user",
                "phone-user",
                "--role",
                "user",
                "--password",
                "phone-password",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
        )
        device_raw = subprocess.check_output(
            [
                sys.executable,
                str(ROOT / "tools" / "vpn_control_app.py"),
                "--db",
                str(db_path),
                "--inventory",
                str(INVENTORY),
                "device-create",
                "phone-user",
                "--device-id",
                "phone-user-android",
                "--platform",
                "android",
                "--json",
            ],
            cwd=ROOT,
            text=True,
        )
        device_token = json.loads(device_raw)["token"]
        enrollment_raw = subprocess.check_output(
            [
                sys.executable,
                str(ROOT / "tools" / "vpn_control_app.py"),
                "--db",
                str(db_path),
                "--inventory",
                str(INVENTORY),
                "enrollment-create",
                "phone-user",
                "--device-id",
                "phone-user-enrolled",
                "--display-name",
                "Phone enrolled",
                "--platform",
                "android",
                "--json",
            ],
            cwd=ROOT,
            text=True,
        )
        enrollment_code = json.loads(enrollment_raw)["code"]
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for_healthz(base_url)
            readiness = fetch_json(f"{base_url}/readyz")
            if readiness.get("ok") is not True:
                raise AssertionError(f"unexpected readiness payload: {readiness!r}")
            if not readiness.get("checks"):
                raise AssertionError(f"readiness checks are empty: {readiness!r}")
            manifest = fetch_json(f"{base_url}/api/control/endpoints")
            endpoints = manifest.get("endpoints") or []
            if not endpoints:
                raise AssertionError("endpoint manifest is empty")
            if not manifest.get("valid_until"):
                raise AssertionError(f"endpoint manifest has no valid_until: {manifest!r}")
            if manifest.get("cache_seconds") != 300:
                raise AssertionError(f"unexpected live manifest cache_seconds: {manifest!r}")
            if endpoints[0].get("role") != "primary":
                raise AssertionError(f"first endpoint is not primary: {endpoints[0]!r}")

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            login_page = fetch_text(opener, f"{base_url}/login")
            for snippet in ("loginForm", "/api/login"):
                if snippet not in login_page:
                    raise AssertionError(f"login page is missing {snippet!r}")
            login = post_json(
                opener,
                f"{base_url}/api/login",
                {"username": "smoke-admin", "password": "smoke-password"},
            )
            if login.get("ok") is not True:
                raise AssertionError(f"login failed: {login!r}")
            admin_page = fetch_text(opener, f"{base_url}/admin")
            for snippet in (
                'id="adminTabs"',
                'data-admin-section="status"',
                'data-admin-section="agents"',
                "globalDefaultPriorityForm",
                "globalRouteAutoText",
                "adminRouteAutoText",
                "adminLookupForm",
                "enrollmentForm",
                "enrollmentCodesBody",
                "agentDiagnosticsBody",
                "agentUpdatesBody",
                "autoProbeJobsBody",
                "providerTransportsBody",
                "adminCriticalServiceRouting",
                "adminCriticalServiceCandidates",
                "scheduleAutoHistory",
                "data-save-agent",
                "data-delete-agent",
                "data-delete-user-mode",
                "Delete account only",
                "Delete + revoke Cudy peer",
                "Apply state",
                "Delete device",
                "Revoke one-time enrollment code",
            ):
                if snippet not in admin_page:
                    raise AssertionError(f"admin page is missing {snippet!r}")
            for forbidden in ("Auto Candidate Lists", "autoCandidatesForm", "/api/admin/client-config", "data-disable-agent"):
                if forbidden in admin_page:
                    raise AssertionError(f"admin page exposes obsolete UI: {forbidden}")
            admin_payload = fetch_json_with_opener(opener, f"{base_url}/api/admin")
            for key in ("servers", "users", "routes", "auto_candidates", "service_aliases", "agent_updates"):
                if key not in admin_payload:
                    raise AssertionError(f"/api/admin is missing {key!r}")
            if "agent_enrollment_codes" not in admin_payload:
                raise AssertionError("/api/admin is missing 'agent_enrollment_codes'")
            lifecycle_user = {
                "id": "lifecycle-user",
                "display_name": "Lifecycle User",
                "role": "user",
                "default_server_id": "auto",
                "client_ip": "",
                "password": "lifecycle-password",
                "enabled": True,
                "create_cudy_client": False,
            }
            created_user = post_json(opener, f"{base_url}/api/admin/users", lifecycle_user)
            if created_user.get("ok") is not True:
                raise AssertionError(f"admin user creation failed: {created_user!r}")
            lifecycle_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            lifecycle_login = post_json(
                lifecycle_opener,
                f"{base_url}/api/login",
                {"username": "lifecycle-user", "password": "lifecycle-password"},
            )
            if lifecycle_login.get("ok") is not True:
                raise AssertionError(f"new user login failed: {lifecycle_login!r}")
            deleted_user = fetch_json_with_opener(
                opener,
                f"{base_url}/api/admin/users?id=lifecycle-user&revoke_cudy=0",
                method="DELETE",
            )
            if deleted_user.get("ok") is not True:
                raise AssertionError(f"admin user deletion failed: {deleted_user!r}")
            expect_http_error(
                lambda: post_json_public(
                    f"{base_url}/api/login",
                    {"username": "lifecycle-user", "password": "lifecycle-password"},
                ),
                401,
            )
            gzip_request = urllib.request.Request(
                f"{base_url}/api/admin",
                headers={"accept-encoding": "gzip"},
            )
            with opener.open(gzip_request, timeout=5) as response:
                if response.headers.get("content-encoding") != "gzip":
                    raise AssertionError("/api/admin did not honor Accept-Encoding: gzip")
                compressed_admin = json.loads(gzip.decompress(response.read()).decode("utf-8"))
            if compressed_admin.get("users") != admin_payload.get("users"):
                raise AssertionError("gzip /api/admin payload differs from identity response")
            saved_alias = post_json(
                opener,
                f"{base_url}/api/service-aliases",
                {"alias": "smoke-alias", "label": "Smoke", "targets": "example.com"},
            )
            if saved_alias.get("ok") is not True:
                raise AssertionError(f"admin alias save failed: {saved_alias!r}")

            user_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            user_login = post_json(
                user_opener,
                f"{base_url}/api/login",
                {"username": "phone-user", "password": "phone-password"},
            )
            if user_login.get("ok") is not True:
                raise AssertionError(f"user login failed: {user_login!r}")
            user_page = fetch_text(user_opener, f"{base_url}/")
            for snippet in (
                'id="aliasForm" class="row"',
                "A local alias replaces the global alias with the same name",
                'id="criticalServiceRouting"',
                'id="criticalServiceCandidates"',
            ):
                if snippet not in user_page:
                    raise AssertionError(f"user page is missing local alias UI: {snippet!r}")
            expect_http_error(
                lambda: post_json(
                    user_opener,
                    f"{base_url}/api/service-aliases",
                    {"alias": "forbidden", "label": "Forbidden", "targets": "example.org"},
                ),
                403,
            )
            local_alias = post_json(
                user_opener,
                f"{base_url}/api/user/service-aliases",
                {"alias": "smoke-alias", "label": "Local Smoke", "targets": "216.239.36.21"},
            )
            if local_alias.get("scope") != "user":
                raise AssertionError(f"local alias save failed: {local_alias!r}")
            alias_payload = fetch_json_with_opener(user_opener, f"{base_url}/api/user/service-aliases")
            effective_aliases = alias_payload.get("effective") or []
            smoke_effective = next((item for item in effective_aliases if item.get("alias") == "smoke-alias"), None)
            if not smoke_effective or smoke_effective.get("scope") != "user" or smoke_effective.get("targets") != ["216.239.36.21/32"]:
                raise AssertionError(f"local alias did not override the global alias: {alias_payload!r}")
            local_lookup = fetch_json_with_opener(user_opener, f"{base_url}/api/route-lookup?target=smoke-alias")
            if local_lookup.get("alias", {}).get("scope") != "user" or [item.get("target") for item in local_lookup.get("results") or []] != ["216.239.36.21/32"]:
                raise AssertionError(f"route lookup did not use the local alias: {local_lookup!r}")

            other_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            other_login = post_json(
                other_opener,
                f"{base_url}/api/login",
                {"username": "other-user", "password": "other-password"},
            )
            if other_login.get("ok") is not True:
                raise AssertionError(f"second user login failed: {other_login!r}")
            other_lookup = fetch_json_with_opener(other_opener, f"{base_url}/api/route-lookup?target=smoke-alias")
            if other_lookup.get("alias", {}).get("scope") != "global" or [item.get("target") for item in other_lookup.get("results") or []] != ["example.com"]:
                raise AssertionError(f"local alias leaked to another user: {other_lookup!r}")

            deleted_local = fetch_json_with_opener(
                user_opener,
                f"{base_url}/api/user/service-aliases?alias=smoke-alias",
                method="DELETE",
            )
            if deleted_local.get("ok") is not True:
                raise AssertionError(f"local alias delete failed: {deleted_local!r}")
            restored_lookup = fetch_json_with_opener(user_opener, f"{base_url}/api/route-lookup?target=smoke-alias")
            if restored_lookup.get("alias", {}).get("scope") != "global" or [item.get("target") for item in restored_lookup.get("results") or []] != ["example.com"]:
                raise AssertionError(f"global alias was not restored after deleting local override: {restored_lookup!r}")
            routed_service = post_json(
                user_opener,
                f"{base_url}/api/critical-services",
                {
                    "service_key": "smoke-suite",
                    "label": "Smoke Suite",
                    "targets": "https://one.smoke.example/, https://two.smoke.example/",
                    "routing_enabled": True,
                    "candidate_server_ids": "proxyde, proxynl",
                    "enabled": True,
                },
            )
            if not routed_service.get("routing_enabled") or routed_service.get("candidate_server_ids") != ["proxyde", "proxynl"]:
                raise AssertionError(f"routed service group save failed: {routed_service!r}")
            service_bootstrap = fetch_json_with_opener(user_opener, f"{base_url}/api/bootstrap")
            effective_services = service_bootstrap.get("critical_services", {}).get("effective") or []
            smoke_service = next((item for item in effective_services if item.get("service_key") == "smoke-suite"), None)
            if not smoke_service or not smoke_service.get("routing_enabled"):
                raise AssertionError(f"routed service group is missing from bootstrap: {service_bootstrap!r}")
            expect_http_error(
                lambda: fetch_json_with_opener(
                    user_opener,
                    f"{base_url}/api/service-aliases?alias=smoke-alias",
                    method="DELETE",
                ),
                403,
            )
            admin_code = post_json(
                opener,
                f"{base_url}/api/admin/enrollment-codes",
                {
                    "user_id": "phone-user",
                    "device_id": "phone-user-admin-code",
                    "display_name": "Admin created Android",
                    "platform": "android",
                    "ttl_hours": 24,
                },
            )
            if admin_code.get("ok") is not True or not admin_code.get("code"):
                raise AssertionError(f"admin enrollment code creation failed: {admin_code!r}")
            revoked_code = fetch_json_with_opener(
                opener,
                f"{base_url}/api/admin/enrollment-codes?id={admin_code['id']}",
                method="DELETE",
            )
            if revoked_code.get("ok") is not True:
                raise AssertionError(f"admin enrollment code revoke failed: {revoked_code!r}")
            expect_http_error(
                lambda: post_json_public(
                    f"{base_url}/api/agent/enroll",
                    {
                        "code": admin_code["code"],
                        "device_id": "phone-user-revoked-code",
                        "display_name": "Revoked code device",
                        "platform": "android",
                    },
                    timeout=15,
                ),
                401,
            )
            winners = fetch_json_with_opener(opener, f"{base_url}/api/admin/auto-winners?target=telegram&limit=10")
            if "winners" not in winners:
                raise AssertionError(f"auto winners payload is malformed: {winners!r}")
            lookup = fetch_json_with_opener(opener, f"{base_url}/api/route-lookup?target=216.239.36.21")
            results = lookup.get("results") or []
            if not results or results[0].get("route_state") != "direct":
                raise AssertionError(f"route lookup should report direct for unmanaged IP: {lookup!r}")

            agent_bootstrap = fetch_json_with_bearer(f"{base_url}/api/agent/bootstrap", device_token)
            if agent_bootstrap.get("user", {}).get("id") != "phone-user":
                raise AssertionError(f"agent bootstrap returned the wrong user: {agent_bootstrap!r}")
            diagnostic = post_json_with_bearer(
                f"{base_url}/api/agent/diagnostics",
                device_token,
                {"summary": "smoke diagnostic", "report": "diagnostic report body"},
            )
            if diagnostic.get("ok") is not True or not diagnostic.get("id"):
                raise AssertionError(f"agent diagnostic save failed: {diagnostic!r}")
            admin_after_diagnostic = fetch_json_with_opener(opener, f"{base_url}/api/admin")
            diagnostics = admin_after_diagnostic.get("agent_diagnostics") or []
            if not diagnostics or diagnostics[0].get("summary") != "smoke diagnostic":
                raise AssertionError(f"admin diagnostics did not include submitted report: {admin_after_diagnostic!r}")
            agent_browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            with agent_browser.open(f"{base_url}/agent-login?token={device_token}", timeout=5) as response:
                response.read()
            browser_bootstrap = fetch_json_with_opener(agent_browser, f"{base_url}/api/bootstrap")
            if browser_bootstrap.get("user", {}).get("id") != "phone-user":
                raise AssertionError(f"agent browser login returned the wrong user: {browser_bootstrap!r}")
            app_version = fetch_json_with_bearer(f"{base_url}/api/agent/app-version?platform=android", device_token)
            if app_version.get("platform") != "android" or "version_code" not in app_version:
                raise AssertionError(f"agent app version payload is malformed: {app_version!r}")
            windows_version = fetch_json_with_bearer(f"{base_url}/api/agent/app-version?platform=windows", device_token)
            if windows_version.get("download_url"):
                request = urllib.request.Request(
                    f"{base_url}{windows_version['download_url']}",
                    headers={"authorization": f"Bearer {device_token}"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    first_bytes = response.read(2)
                if first_bytes != b"PK":
                    raise AssertionError(f"windows update package is not a zip: {first_bytes!r}")
            agent_lookup = fetch_json_with_bearer(
                f"{base_url}/api/agent/route-lookup?target=216.239.36.21",
                device_token,
            )
            agent_results = agent_lookup.get("results") or []
            if not agent_results or agent_results[0].get("route_state") != "direct":
                raise AssertionError(f"agent route lookup should report direct for unmanaged IP: {agent_lookup!r}")
            default_result = post_json_with_bearer(
                f"{base_url}/api/agent/user-default-server",
                device_token,
                {"server_id": "auto", "auto_candidate_server_ids": ["proxyde", "all-rest"]},
            )
            if default_result.get("ok") is not True:
                raise AssertionError(f"agent default save failed: {default_result!r}")
            domain_result = post_json_with_bearer(
                f"{base_url}/api/agent/domain-routes",
                device_token,
                {"domain": "gemini.google.com", "server_id": "auto", "auto_candidate_server_ids": ["proxyde", "all-rest"]},
            )
            if domain_result.get("ok") is not True:
                raise AssertionError(f"agent domain save failed: {domain_result!r}")
            refreshed_bootstrap = fetch_json_with_bearer(f"{base_url}/api/agent/bootstrap", device_token)
            if refreshed_bootstrap.get("user", {}).get("default_server_id") != "auto":
                raise AssertionError(f"agent default was not persisted: {refreshed_bootstrap!r}")
            saved_routes = refreshed_bootstrap.get("routes") or []
            if not any(item.get("domain") == "gemini.google.com" for item in saved_routes):
                raise AssertionError(f"agent domain route was not persisted: {refreshed_bootstrap!r}")

            enrollment = post_json_public(
                f"{base_url}/api/agent/enroll",
                {
                    "code": enrollment_code,
                    "device_id": "phone-user-enrolled",
                    "display_name": "Phone enrolled",
                    "platform": "android",
                },
                timeout=15,
            )
            enrolled_token = enrollment.get("token")
            if enrollment.get("ok") is not True or not enrolled_token:
                raise AssertionError(f"enrollment failed: {enrollment!r}")
            expect_http_error(
                lambda: post_json_public(
                    f"{base_url}/api/agent/enroll",
                    {
                        "code": enrollment_code,
                        "device_id": "phone-user-enrolled-again",
                        "display_name": "Phone enrolled again",
                        "platform": "android",
                    },
                    timeout=15,
                ),
                401,
            )
            enrolled_bootstrap = fetch_json_with_bearer(f"{base_url}/api/agent/bootstrap", enrolled_token)
            if enrolled_bootstrap.get("user", {}).get("id") != "phone-user":
                raise AssertionError(f"enrolled token returned the wrong user: {enrolled_bootstrap!r}")
            disabled_device = post_json(
                opener,
                f"{base_url}/api/admin/agent-devices",
                {"id": "phone-user-enrolled", "enabled": False},
            )
            if disabled_device.get("ok") is not True or disabled_device.get("enabled") is not False:
                raise AssertionError(f"agent device disable failed: {disabled_device!r}")
            if int(disabled_device.get("cached_tokens_invalidated") or 0) < 1:
                raise AssertionError(f"agent device disable did not invalidate its cached token: {disabled_device!r}")
            expect_http_error(
                lambda: fetch_json_with_bearer(f"{base_url}/api/agent/bootstrap", enrolled_token, timeout=15),
                401,
            )
            enabled_device = post_json(
                opener,
                f"{base_url}/api/admin/agent-devices",
                {"id": "phone-user-enrolled", "enabled": True},
            )
            if enabled_device.get("ok") is not True or enabled_device.get("enabled") is not True:
                raise AssertionError(f"agent device enable failed: {enabled_device!r}")
            reenabled_bootstrap = fetch_json_with_bearer(f"{base_url}/api/agent/bootstrap", enrolled_token)
            if reenabled_bootstrap.get("user", {}).get("id") != "phone-user":
                raise AssertionError(f"re-enabled agent token did not recover: {reenabled_bootstrap!r}")
            deleted_device = fetch_json_with_opener(
                opener,
                f"{base_url}/api/admin/agent-devices?id=phone-user-enrolled&hard=1",
                method="DELETE",
            )
            if deleted_device.get("ok") is not True or deleted_device.get("deleted") is not True:
                raise AssertionError(f"agent device delete failed: {deleted_device!r}")
            expect_http_error(
                lambda: fetch_json_with_bearer(f"{base_url}/api/agent/bootstrap", enrolled_token, timeout=15),
                401,
            )

            status_raw = subprocess.check_output(
                [
                    sys.executable,
                    str(ROOT / "tools" / "vpn_control_app.py"),
                    "--db",
                    str(db_path),
                    "--inventory",
                    str(INVENTORY),
                    "system-status",
                    "--json",
                    "--strict",
                ],
                cwd=ROOT,
                text=True,
            )
            status = json.loads(status_raw)
            workers = status.get("workers") or {}
            for name in ("auto_probe", "provider_refresh"):
                if name not in workers:
                    raise AssertionError(f"{name} worker status was not persisted")
                if workers[name].get("enabled") is not False:
                    raise AssertionError(f"{name} worker status should be disabled: {workers[name]!r}")
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate(timeout=10)
            if proc.returncode not in (0, -15, 1):
                raise RuntimeError(f"control server exited unexpectedly: {proc.returncode}")

    print("Control server HTTP smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
