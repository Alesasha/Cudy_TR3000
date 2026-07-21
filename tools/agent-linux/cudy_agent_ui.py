#!/usr/bin/env python3
"""Small persistent desktop UI for the Linux managed agent."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, Y, Canvas, Entry, Listbox, PhotoImage, Scrollbar, StringVar, Tk, Text, messagebox
from tkinter import ttk


ROOT = Path(__file__).resolve().parent
AGENT_ENV = {}


def read_agent_env() -> dict[str, str]:
    result: dict[str, str] = {}
    env_path = ROOT / "agent.env"
    if not env_path.exists():
        return result
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip().strip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


AGENT_ENV = read_agent_env()
SERVICE_NAME = os.environ.get("SERVICE_NAME") or AGENT_ENV.get("SERVICE_NAME") or "cudy-managed-agent.service"
CONTROL_LOCAL_PORT = os.environ.get("CONTROL_LOCAL_PORT") or AGENT_ENV.get("CONTROL_LOCAL_PORT") or "18765"
VPN_AGENT_TOKEN = os.environ.get("VPN_AGENT_TOKEN") or AGENT_ENV.get("VPN_AGENT_TOKEN") or ""
AGENT_PLATFORM = os.environ.get("AGENT_PLATFORM") or AGENT_ENV.get("AGENT_PLATFORM") or "linux"
CONTROL_START_GRACE_SECONDS = int(os.environ.get("CONTROL_START_GRACE_SECONDS") or AGENT_ENV.get("CONTROL_START_GRACE_SECONDS") or "90")
VERSION_FILE = ROOT / (os.environ.get("AGENT_VERSION_FILE") or AGENT_ENV.get("AGENT_VERSION_FILE") or "agent.version.json")
UPDATE_STATUS_RAW = os.environ.get("AGENT_UPDATE_STATUS_FILE") or AGENT_ENV.get("AGENT_UPDATE_STATUS_FILE") or "run/update-status.txt"
UPDATE_STATUS_FILE = Path(UPDATE_STATUS_RAW)
if not UPDATE_STATUS_FILE.is_absolute():
    UPDATE_STATUS_FILE = ROOT / UPDATE_STATUS_FILE
UPDATE_MARKER_RAW = os.environ.get("AGENT_UPDATE_MARKER_FILE") or AGENT_ENV.get("AGENT_UPDATE_MARKER_FILE") or "run/update-in-progress.json"
UPDATE_MARKER_FILE = Path(UPDATE_MARKER_RAW)
if not UPDATE_MARKER_FILE.is_absolute():
    UPDATE_MARKER_FILE = ROOT / UPDATE_MARKER_FILE

VPN_INTERFACE_PREFIXES = ("proxy", "lokvpn", "amn", "awg", "wg", "tun", "sing")
UPDATE_ACTION_LABELS = {"Update Agent", "Update / Repair"}


def run(command: list[str], *, timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return (
            1,
            (
                f"Command {command!r} timed out after {timeout} seconds.\n"
                "If a system authorization prompt was shown, approve it and retry.\n"
                "If no prompt was shown, run this action from the terminal shortcut."
            ),
        )
    except Exception as exc:  # keep the UI alive even if a helper fails
        return 1, str(exc)


def control_health() -> bool:
    rc, _ = run(
        ["curl", "-fsS", "--connect-timeout", "2", "--max-time", "4", f"http://127.0.0.1:{CONTROL_LOCAL_PORT}/healthz"],
        timeout=6,
    )
    return rc == 0


def service_active_age_seconds() -> float | None:
    rc, raw = run(["systemctl", "show", SERVICE_NAME, "-p", "ActiveEnterTimestampMonotonic", "--value"], timeout=6)
    if rc != 0:
        return None
    try:
        active_usec = int(raw.strip() or "0")
    except ValueError:
        return None
    if active_usec <= 0:
        return None
    return max(0.0, time.monotonic() - (active_usec / 1_000_000))


def watchdog_recovery_pending() -> bool:
    path = ROOT / "run" / "watchdog-tripped.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload.get("retry_after_epoch") or 0) > int(time.time())
    except (OSError, ValueError, TypeError):
        return False


def service_info() -> dict[str, str | bool | float | None]:
    active_rc, active = run(["systemctl", "is-active", SERVICE_NAME], timeout=6)
    enabled_rc, enabled = run(["systemctl", "is-enabled", SERVICE_NAME], timeout=6)
    control_ok = control_health()
    active_age = service_active_age_seconds()
    update_status = read_update_status()
    active_value = active if active_rc == 0 else active or "unknown"
    enabled_value = enabled if enabled_rc == 0 else enabled or "unknown"
    updating = update_in_progress(update_status) if update_status else False
    if updating:
        state = "warn"
        title = "UPDATING"
        comment = "Updating agent; internet may be briefly unavailable"
    elif active_value != "active" and enabled_value == "enabled" and watchdog_recovery_pending():
        state = "warn"
        title = "RECOVERY"
        comment = "Direct internet restored; retrying agent soon"
    elif active_value != "active" and enabled_value == "enabled":
        state = "warn"
        title = "STARTING"
        comment = "Agent service is waiting for automatic restart"
    elif active_value != "active":
        state = "off"
        title = "OFF"
        comment = "Agent is stopped"
    elif control_ok:
        state = "ok"
        title = "OK"
        comment = "Connection is healthy"
    elif active_age is not None and active_age < CONTROL_START_GRACE_SECONDS:
        state = "warn"
        title = "STARTING"
        comment = "Connecting..."
    else:
        state = "down"
        title = "DOWN"
        comment = "Control link is lost"
    return {
        "active": active_value,
        "enabled": enabled_value,
        "control_ok": control_ok,
        "active_age": active_age,
        "update_status": update_status,
        "updating": updating,
        "state": state,
        "title": title,
        "comment": comment,
    }


def read_managed_traffic_counters() -> dict[str, int]:
    counters: dict[str, int] = {}
    proc_net_dev = Path("/proc/net/dev")
    try:
        lines = proc_net_dev.read_text(encoding="utf-8", errors="replace").splitlines()[2:]
    except Exception:
        return counters
    for line in lines:
        if ":" not in line:
            continue
        raw_name, raw_values = line.split(":", 1)
        name = raw_name.strip()
        if not name.startswith(VPN_INTERFACE_PREFIXES):
            continue
        fields = raw_values.split()
        if len(fields) < 16:
            continue
        try:
            counters[name] = int(fields[0]) + int(fields[8])
        except ValueError:
            continue
    return counters


def format_mb(byte_count: int) -> str:
    mb = byte_count / (1024 * 1024)
    if mb >= 100:
        return f"{mb:.0f} MB"
    if mb >= 10:
        return f"{mb:.1f} MB"
    return f"{mb:.2f} MB"


def read_update_status() -> str:
    try:
        if not UPDATE_STATUS_FILE.exists():
            return ""
        lines = [line.strip() for line in UPDATE_STATUS_FILE.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def update_in_progress(status: str) -> bool:
    try:
        marker = json.loads(UPDATE_MARKER_FILE.read_text(encoding="utf-8"))
        marker_age = time.time() - int(marker.get("updated_at_epoch") or 0)
        if 0 <= marker_age <= 1800:
            return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        if time.time() - UPDATE_STATUS_FILE.stat().st_mtime > 600:
            return False
    except OSError:
        return False
    lowered = status.lower()
    return any(token in lowered for token in ("downloading", "applying", "installing", "stopping", "restarting", "apply process started"))


def policy_sync_status() -> str:
    policy_path = ROOT / "run" / "fresh-config.json"
    try:
        revision = hashlib.sha256(policy_path.read_bytes()).hexdigest()[:8]
        stamp = datetime.fromtimestamp(policy_path.stat().st_mtime).strftime("%H:%M:%S")
        return f"Routing rules: {revision}, synchronized at {stamp}"
    except OSError:
        return "Routing rules: waiting for first sync"


def software_update_status(status: str) -> str:
    lowered = status.lower()
    if update_in_progress(status):
        if "downloading" in lowered:
            return "Software update: downloading"
        if "installing" in lowered or "applying" in lowered or "apply process started" in lowered:
            return "Software update: installing"
        if "restarting" in lowered or "stopping" in lowered:
            return "Software update: restarting service"
    if "failed" in lowered:
        return "Software update: failed; open Diagnostics"
    return ""


def current_version() -> tuple[str, int]:
    if VERSION_FILE.exists():
        try:
            payload = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
            return str(payload.get("version_name") or ""), int(payload.get("version_code") or 0)
        except Exception:
            pass
    return str(AGENT_ENV.get("AGENT_VERSION_NAME") or ""), int(AGENT_ENV.get("AGENT_VERSION_CODE") or 0)


def latest_version() -> tuple[str, int] | None:
    url = f"http://127.0.0.1:{CONTROL_LOCAL_PORT}/api/agent/app-version?platform={AGENT_PLATFORM}"
    request = urllib.request.Request(url)
    if VPN_AGENT_TOKEN:
        request.add_header("Authorization", "Bearer " + VPN_AGENT_TOKEN)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("version_name") or ""), int(payload.get("version_code") or 0)
    except Exception:
        return None


def launch_fresh_ui() -> None:
    launcher = ROOT / "cudy_agent_ui.sh"
    command = [str(launcher)] if launcher.exists() else ["python3", str(ROOT / "cudy_agent_ui.py")]
    subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def update_started(output: str, rc: int) -> bool:
    if rc == 10:
        return True
    lowered = output.lower()
    return any(
        token in lowered
        for token in (
            "apply process started",
            "update downloaded",
            "applying staged update",
            "self-update",
        )
    )


class AgentUi:
    def __init__(self) -> None:
        self.root = Tk(className="CudyAgent")
        self.root.title("Cudy Agent")
        self.root.geometry("900x700")
        self.root.minsize(820, 620)
        self.status_line = StringVar(value="Starting...")
        self.comment_line = StringVar(value="")
        self.version_status = StringVar(value="Version: checking...")
        self.speed_url = StringVar(value="")
        self.current_version_code = 0
        self.latest_version_code = 0
        self.loaded_version_code = current_version()[1]
        self.relaunching = False
        self.traffic_counters = read_managed_traffic_counters()
        self.traffic_delta = 0
        self.busy = False
        self.restart_after_update = False
        self.status_icons = self.build_status_icons()
        self.actions = [
            ("ON", ["./agent_on.sh"], 180),
            ("OFF", ["./agent_off.sh"], 180),
            ("OFF + Exit", ["./agent_off.sh"], 180),
            ("Status", ["./status.sh"], 30),
            ("Diagnostics", ["./run_diagnostics.sh"], 180),
            ("Fast Speed", ["./run_speed_tests.sh", "--quick"], 150),
            ("Full Speed", ["./run_speed_tests.sh"], 240),
            ("URL Test", self.speed_test_command, 180),
            ("Settings", ["./open_user_ui.sh"], 30),
            ("Update / Repair", self.force_update_command, 300),
            ("Exit UI", self.exit_ui_command, 5),
        ]
        self.build()
        self.refresh_status()
        self.root.after(15000, self.refresh_status_periodic)

    @staticmethod
    def build_status_icons() -> dict[str, PhotoImage]:
        colors = {
            "ok": "#1f9d55",
            "warn": "#d97706",
            "down": "#dc2626",
            "off": "#111827",
        }
        icons: dict[str, PhotoImage] = {}
        size = 64
        center = (size - 1) / 2
        radius_sq = 29 * 29
        inner_sq = 22 * 22
        for state, color in colors.items():
            icon = PhotoImage(width=size, height=size)
            for y in range(size):
                for x in range(size):
                    distance_sq = (x - center) ** 2 + (y - center) ** 2
                    if distance_sq <= radius_sq:
                        icon.put(color if distance_sq > inner_sq else "#ffffff", (x, y))
                    else:
                        icon.transparency_set(x, y, True)
            icons[state] = icon
        return icons

    def build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill=BOTH, expand=True)

        ttk.Label(outer, text="Cudy Agent", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")

        dashboard = ttk.Frame(outer)
        dashboard.pack(fill="x", pady=(8, 12))
        self.indicator = Canvas(dashboard, width=164, height=164, highlightthickness=0)
        self.indicator.pack(side=LEFT, padx=(0, 18))
        dashboard_text = ttk.Frame(dashboard)
        dashboard_text.pack(side=LEFT, fill="x", expand=True)
        ttk.Label(dashboard_text, textvariable=self.status_line, font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(dashboard_text, textvariable=self.comment_line, font=("TkDefaultFont", 11)).pack(anchor="w", pady=(4, 0))

        version_row = ttk.Frame(outer)
        version_row.pack(fill="x", pady=(0, 10))
        ttk.Label(version_row, textvariable=self.version_status).pack(side=LEFT)
        self.update_button = ttk.Button(version_row, text="Update", command=self.run_update, state="disabled")
        self.update_button.pack(side=RIGHT)

        url_row = ttk.Frame(outer)
        url_row.pack(fill="x", pady=(0, 10))
        ttk.Label(url_row, text="Custom URL (optional)").pack(side=LEFT, padx=(0, 8))
        Entry(url_row, textvariable=self.speed_url).pack(side=LEFT, fill="x", expand=True)

        middle = ttk.Frame(outer)
        middle.pack(fill=BOTH, expand=True)

        self.listbox = Listbox(middle, width=24, height=12, activestyle="dotbox", exportselection=False)
        for label, _, _ in self.actions:
            self.listbox.insert(END, label)
        self.listbox.selection_set(0)
        self.listbox.bind("<Double-Button-1>", lambda _event: self.run_selected())
        self.listbox.bind("<Return>", lambda _event: self.run_selected())
        self.listbox.pack(side=LEFT, fill=Y, padx=(0, 12))

        scroll = Scrollbar(middle)
        scroll.pack(side=RIGHT, fill=Y)
        self.output = Text(middle, wrap="word", yscrollcommand=scroll.set)
        self.output.pack(fill=BOTH, expand=True)
        scroll.config(command=self.output.yview)
        self.write_output("Double-click an action on the left.\n")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="Window close exits UI only. Turn OFF stops the service.").pack(side=LEFT)
        ttk.Button(footer, text="Copy Result", command=self.copy_output).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(footer, text="Close Window", command=self.root.destroy).pack(side=RIGHT)

    def write_output(self, text: str) -> None:
        self.output.insert(END, text.rstrip() + "\n")
        self.output.see(END)

    def copy_output(self) -> None:
        text = self.output.get("1.0", END).strip()
        if not text:
            messagebox.showinfo("Cudy Agent", "There is no result to copy yet.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        messagebox.showinfo("Cudy Agent", "Result copied to clipboard.")

    def draw_indicator(self, state: str, title: str) -> None:
        colors = {
            "ok": ("#1f9d55", "#ffffff"),
            "warn": ("#d97706", "#ffffff"),
            "down": ("#dc2626", "#ffffff"),
            "off": ("#111827", "#ffffff"),
        }
        fill, text_color = colors.get(state, colors["down"])
        status_icon = self.status_icons.get(state, self.status_icons["down"])
        self.root.iconphoto(True, status_icon)
        self.indicator.delete("all")
        self.indicator.create_oval(8, 8, 156, 156, fill=fill, outline=fill)
        self.indicator.create_text(82, 62, text=title, fill=text_color, font=("TkDefaultFont", 20, "bold"))
        self.indicator.create_text(82, 100, text=format_mb(self.traffic_delta), fill=text_color, font=("TkDefaultFont", 14, "bold"))
        self.indicator.create_text(82, 124, text="this session", fill=text_color, font=("TkDefaultFont", 9))

    def refresh_status(self) -> None:
        info = service_info()
        current_counters = read_managed_traffic_counters()
        for name, current_value in current_counters.items():
            previous_value = self.traffic_counters.get(name)
            if previous_value is not None and current_value >= previous_value:
                self.traffic_delta += current_value - previous_value
        self.traffic_counters = current_counters
        self.draw_indicator(str(info["state"]), str(info["title"]))
        self.status_line.set(str(info["comment"]))
        self.listbox.config(state="disabled" if info["updating"] else "normal")
        details = [
            f"Service: {info['active']}",
            f"Autostart: {info['enabled']}",
            f"Control: {'OK' if info['control_ok'] else 'not connected'}",
            f"Traffic: {format_mb(self.traffic_delta)}",
        ]
        details.append(policy_sync_status())
        update_status = software_update_status(str(info["update_status"] or ""))
        if update_status:
            details.append(update_status)
        self.comment_line.set("  |  ".join(details))
        current_name, current_code = current_version()
        latest = latest_version()
        self.current_version_code = current_code
        if (
            self.loaded_version_code > 0
            and current_code > 0
            and current_code != self.loaded_version_code
            and not info["updating"]
            and not self.relaunching
        ):
            self.relaunching = True
            self.write_output("Agent software was updated. Restarting this window to load the new UI...")
            self.root.after(250, self.restart_window_after_background_update)
            return
        if latest is None:
            self.latest_version_code = 0
            self.version_status.set(f"Software: installed {current_name or '-'} | latest unavailable")
            self.update_button.config(state="disabled")
            return
        latest_name, latest_code = latest
        self.latest_version_code = latest_code
        self.version_status.set(f"Software: installed {current_name or '-'} | latest {latest_name or '-'}")
        self.update_button.config(state="normal" if latest_code > current_code and not info["updating"] else "disabled")

    def restart_window_after_background_update(self) -> None:
        try:
            launch_fresh_ui()
            self.root.destroy()
        except Exception as exc:
            self.relaunching = False
            self.write_output(f"Automatic UI restart failed: {exc}")

    def refresh_status_periodic(self) -> None:
        if not self.busy:
            self.refresh_status()
        self.root.after(5000, self.refresh_status_periodic)

    def run_selected(self) -> None:
        if self.busy:
            messagebox.showinfo("Cudy Agent", "Another action is still running.")
            return
        selection = self.listbox.curselection()
        if not selection:
            return
        label, command, timeout = self.actions[selection[0]]
        if callable(command):
            command = command()
            if not command:
                return
        self.busy = True
        self.write_output(f"\n[{time.strftime('%H:%M:%S')}] {label}...")
        threading.Thread(target=self.worker, args=(label, command, timeout), daemon=True).start()

    def speed_test_command(self) -> list[str] | None:
        url = self.speed_url.get().strip()
        if not url:
            return ["./run_speed_tests.sh", "--quick"]
        if "://" not in url:
            url = "https://" + url
            self.speed_url.set(url)
        return ["./run_speed_tests.sh", "--only-url", url]

    def run_update(self) -> None:
        if self.busy:
            messagebox.showinfo("Cudy Agent", "Another action is still running.")
            return
        if self.latest_version_code <= self.current_version_code:
            messagebox.showinfo("Cudy Agent", "Agent is already up to date.")
            return
        self.busy = True
        self.restart_after_update = True
        self.write_output(f"\n[{time.strftime('%H:%M:%S')}] Update Agent...")
        self.write_output("Update is in progress. Some services may be temporarily unavailable until it finishes.")
        threading.Thread(target=self.worker, args=("Update Agent", ["./update_agent.sh"], 300), daemon=True).start()

    def force_update_command(self) -> list[str] | None:
        if not messagebox.askyesno(
            "Cudy Agent",
            "Force reinstall the latest agent package?\n\nSome services may be temporarily unavailable until it finishes.",
        ):
            return None
        self.restart_after_update = True
        return ["./update_agent.sh", "--force"]

    def wait_for_update_and_restart(self, previous_code: int) -> None:
        deadline = time.monotonic() + 180
        completed = False
        failure = ""
        while time.monotonic() < deadline:
            status = read_update_status().lower()
            if "completed current=" in status:
                completed = True
                break
            if "failed" in status:
                failure = read_update_status()
                break
            time.sleep(2)

        def restart() -> None:
            if not completed:
                detail = failure or "Update did not finish within 180 seconds."
                self.write_output(detail)
                self.write_output("The current window remains open. Run Diagnostics before retrying the update.")
                self.restart_after_update = False
                self.busy = False
                self.refresh_status()
                return
            try:
                self.write_output("Update installed. Restarting Cudy Agent window...")
                launch_fresh_ui()
                self.root.destroy()
            except Exception as exc:
                self.write_output(f"Update installed, but UI restart failed: {exc}")
                self.write_output("Close this window and open Cudy Agent again.")
                self.busy = False

        self.root.after(0, restart)

    def worker(self, label: str, command: list[str], timeout: int) -> None:
        previous_code = self.current_version_code
        rc, output = run(command, timeout=timeout)

        def finish() -> None:
            self.write_output(output or f"{label}: no output")
            ok = rc == 0 or (label in UPDATE_ACTION_LABELS and rc == 10)
            self.write_output(f"{label}: {'OK' if ok else 'FAILED'}")
            self.refresh_status()
            if label in UPDATE_ACTION_LABELS and ok and self.restart_after_update and update_started(output, rc):
                self.write_output("Waiting for updated files, then this window will restart automatically...")
                threading.Thread(target=self.wait_for_update_and_restart, args=(previous_code,), daemon=True).start()
                return
            self.restart_after_update = False
            self.busy = False
            if label == "OFF + Exit" and rc == 0:
                self.root.destroy()

        self.root.after(0, finish)

    def exit_ui_command(self) -> None:
        self.root.destroy()
        return None

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    AgentUi().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
