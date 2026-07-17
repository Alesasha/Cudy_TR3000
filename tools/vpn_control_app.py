#!/usr/bin/env python3
"""
Local VPN routing control web app.

This is intentionally stdlib-only: SQLite + http.server. It provides the first
usable control layer over config/vpn_inventory.json without changing live Cudy
routing yet.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vpn_inventory.json"
DEFAULT_DB = ROOT / "data" / "vpn_control.db"
DEFAULT_USER_ID = "default"
DEFAULT_PBR_EXPORT_DIR = ROOT / "build" / "pbr-overrides"
DEFAULT_USER_ROUTES_EXPORT_DIR = ROOT / "build" / "user-routes"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_VPNTYPE_AUTH_FILE = ROOT / "secrets" / "vpntype_auth.txt"
DEFAULT_VPNTYPE_UUID_FILE = ROOT / "secrets" / "vpntype_uuid.txt"
DEFAULT_LOKVPN_SUB_URL_FILE = ROOT / "secrets" / "lokvpn_sub_url.txt"
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_USER = "root"
DEFAULT_CONTROL_PRIMARY_URL = "http://127.0.0.1:8765"
DEFAULT_CONTROL_PRIMARY_SSH_HOST = "95.182.91.203"
DEFAULT_CONTROL_PRIMARY_SSH_USER = "cudy-tunnel-windows"
DEFAULT_CONTROL_FALLBACK_URLS = "http://10.77.0.1:8765,http://192.168.8.1:8765"
AGENT_TOKEN_CACHE_SECONDS = 300
DEFAULT_CUDY_FRIEND_ENDPOINT = "195.170.35.108:51830"
CUDY_CLIENT_OUTPUT_DIR = ROOT / "secrets" / "clients" / "cudy-home"
REMOTE_PBR_DIR = "/etc/pbr-overrides"
REMOTE_USER_ROUTES_DIR = "/etc/cudy-user-routes"
REMOTE_PBR_SCRIPT = "/usr/share/pbr/pbr.user.opencck-merged-vpn"
LOCAL_PBR_SCRIPT = ROOT / "openwrt" / "pbr.user.opencck-merged-vpn"
LOCAL_SWITCHER_INSTALLER = ROOT / "openwrt" / "install-vpn-switchers.sh"
LOCAL_USER_ROUTES_APPLY = ROOT / "openwrt" / "cudy-user-routes-apply"
LOCAL_USER_ROUTES_INIT = ROOT / "openwrt" / "cudy-user-routes.init"
SESSION_COOKIE = "vpn_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 210_000
DEVICE_TOKEN_PREFIX = "vca_"
ENROLLMENT_CODE_PREFIX = "cudy-"
APP_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
CUDY_FALLBACK_STATE_URL = os.environ.get("CUDY_FALLBACK_STATE_URL", "http://192.168.8.1/cudy-control/state.json")
CUDY_FALLBACK_MAX_AGE_SECONDS = int(os.environ.get("CUDY_FALLBACK_MAX_AGE_SECONDS", "3600"))
CUDY_FALLBACK_STATUS_WARN = os.environ.get("CUDY_FALLBACK_STATUS_WARN", "").strip().lower() in {"1", "true", "yes", "on"}
CONTROL_BACKUP_DIR = ROOT / "backups" / "control-server"
AGENT_UPDATE_DIR = ROOT / "build" / "agent-updates"
CONTROL_BACKUP_MAX_AGE_SECONDS = int(os.environ.get("CONTROL_BACKUP_MAX_AGE_SECONDS", str(36 * 60 * 60)))
CONTROL_BACKUP_STATUS_WARN = os.environ.get("CONTROL_BACKUP_STATUS_WARN", "").strip().lower() in {"1", "true", "yes", "on"}
LOCAL_FALLBACK_SYNC_STATUS_WARN = os.environ.get("LOCAL_FALLBACK_SYNC_STATUS_WARN", "").strip().lower() in {"1", "true", "yes", "on"}
TRANSPORT_STALE_WARN_SECONDS = int(os.environ.get("TRANSPORT_STALE_WARN_SECONDS", str(24 * 60 * 60)))
PROBE_FAILED_WARN_SECONDS = int(os.environ.get("PROBE_FAILED_WARN_SECONDS", str(60 * 60)))
ANDROID_PROBE_TRANSPORT_WARM_SECONDS = int(
    os.environ.get("ANDROID_PROBE_TRANSPORT_WARM_SECONDS", str(6 * 60 * 60))
)
ANDROID_PROBE_TRANSPORT_WARM_LIMIT = int(os.environ.get("ANDROID_PROBE_TRANSPORT_WARM_LIMIT", "64"))
ANDROID_SUPPORTED_TRANSPORT_TYPES = {"http-proxy-tun", "vless-reality-tun", "sing-box-json"}
WORKER_STATUS_LOCK = threading.Lock()
FALLBACK_STATUS_CACHE_LOCK = threading.Lock()
FALLBACK_STATUS_CACHE: dict[str, Any] = {"checked_at": 0.0, "value": None}
FALLBACK_STATUS_CACHE_SECONDS = int(os.environ.get("CUDY_FALLBACK_STATUS_CACHE_SECONDS", "60"))
WORKER_STATUS: dict[str, dict[str, Any]] = {
    "auto_probe": {"enabled": False, "last_started_at": None, "last_finished_at": None, "last_error": None},
    "provider_refresh": {"enabled": False, "last_started_at": None, "last_finished_at": None, "last_error": None},
}
AUTO_ALL_REST = "all-rest"
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
SAFE_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
TELEGRAM_CIDRS = [
    "149.154.160.0/20",
    "91.105.192.0/23",
    "91.108.12.0/22",
    "91.108.16.0/22",
    "91.108.20.0/22",
    "91.108.4.0/22",
    "91.108.56.0/22",
    "91.108.8.0/22",
]
TELEGRAM_PROBE_URL = "tcp://149.154.167.50:443"
SERVICE_ALIAS_SEEDS = [
    {
        "aliases": ["telegram", "tg", "телеграм"],
        "label": "Telegram",
        "targets": TELEGRAM_CIDRS,
    },
    {
        "aliases": ["youtube", "yt", "ютуб"],
        "label": "YouTube",
        "targets": [
            "youtube.com",
            "www.youtube.com",
            "youtu.be",
            "googlevideo.com",
            "ytimg.com",
            "youtubei.googleapis.com",
        ],
    },
    {
        "aliases": ["gemini", "google-ai", "гемини", "джемини"],
        "label": "Gemini",
        "targets": [
            "gemini.google.com",
            "aistudio.google.com",
        ],
    },
    {
        "aliases": ["chatgpt", "openai", "gpt", "чатгпт", "чатжпт"],
        "label": "ChatGPT / OpenAI",
        "targets": [
            "chatgpt.com",
            "chat.openai.com",
            "openai.com",
            "auth.openai.com",
            "platform.openai.com",
            "cdn.openai.com",
            "oaistatic.com",
            "oaiusercontent.com",
        ],
    },
    {
        "aliases": ["mailru", "mail.ru", "mail", "майл", "мэйл"],
        "label": "Mail.ru",
        "targets": [
            "mail.ru",
            "e.mail.ru",
            "smtp.mail.ru",
            "imap.mail.ru",
            "pop.mail.ru",
        ],
    },
    {
        "aliases": ["gosuslugi", "gosuslugi.ru", "esia"],
        "label": "Gosuslugi",
        "targets": [
            "gosuslugi.ru",
            "www.gosuslugi.ru",
            "esia.gosuslugi.ru",
            "lk.gosuslugi.ru",
        ],
    },
    {
        "aliases": ["speedtest", "спидтест"],
        "label": "Speedtest",
        "targets": [
            "speedtest.net",
            "www.speedtest.net",
        ],
    },
    {
        "aliases": ["linux-mirrors", "linuxmint-mirrors", "ubuntu-mirrors", "зеркала"],
        "label": "Linux mirrors",
        "targets": [
            "mirror.yandex.ru",
            "mirror.logol.ru",
        ],
    },
    {
        "aliases": ["reuters", "reuters.com"],
        "label": "Reuters",
        "targets": [
            "reuters.com",
            "www.reuters.com",
            "www.reutersmedia.net",
        ],
    },
]
MANAGED_GLOBAL_DOMAIN_ROUTE_SEEDS = [
    {
        "server_id": "auto",
        "note": "Managed AI service Auto route",
        "domains": [
            "gemini.google.com",
            "aistudio.google.com",
            "chatgpt.com",
            "chat.openai.com",
            "openai.com",
            "auth.openai.com",
            "platform.openai.com",
            "cdn.openai.com",
            "oaistatic.com",
            "oaiusercontent.com",
        ],
    },
    {
        "server_id": "direct",
        "note": "Russian state service direct route",
        "domains": [
            "gosuslugi.ru",
            "www.gosuslugi.ru",
            "esia.gosuslugi.ru",
            "lk.gosuslugi.ru",
        ],
    },
    {
        "server_id": "auto",
        "note": "Managed Reuters Auto route",
        "domains": [
            "reuters.com",
            "www.reuters.com",
            "www.reutersmedia.net",
        ],
    },
    {
        "server_id": "auto",
        "note": "Managed YouTube Auto route",
        "domains": [
            "youtube.com",
            "www.youtube.com",
            "youtu.be",
            "googlevideo.com",
            "ytimg.com",
            "youtubei.googleapis.com",
        ],
    },
]
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


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS servers (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  provider TEXT NOT NULL,
  kind TEXT NOT NULL,
  interface TEXT,
  geo_country TEXT,
  geo_region TEXT,
  endpoint TEXT,
  switch_command TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  user_visible INTEGER NOT NULL DEFAULT 1,
  admin_visible INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  default_server_id TEXT NOT NULL DEFAULT 'auto',
  client_ip TEXT,
  password_salt TEXT,
  password_hash TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(default_server_id) REFERENCES servers(id)
);

CREATE TABLE IF NOT EXISTS user_domain_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  server_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, domain),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(server_id) REFERENCES servers(id)
);

CREATE TABLE IF NOT EXISTS user_ip_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  target_cidr TEXT NOT NULL,
  server_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, target_cidr),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(server_id) REFERENCES servers(id)
);

CREATE TABLE IF NOT EXISTS global_domain_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT NOT NULL UNIQUE,
  server_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(server_id) REFERENCES servers(id)
);

CREATE TABLE IF NOT EXISTS global_ip_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_cidr TEXT NOT NULL UNIQUE,
  server_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(server_id) REFERENCES servers(id)
);

CREATE TABLE IF NOT EXISTS domain_auto_cache (
  domain TEXT PRIMARY KEY,
  selected_server_id TEXT,
  score_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'unknown',
  checked_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(selected_server_id) REFERENCES servers(id)
);

CREATE TABLE IF NOT EXISTS auto_candidate_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL DEFAULT '',
  candidate_server_ids TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, domain)
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_devices (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  token_salt TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_seen_at TEXT,
  last_ip TEXT,
  last_user_agent TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_status (
  device_id TEXT PRIMARY KEY,
  status_json TEXT NOT NULL,
  reported_at TEXT NOT NULL,
  FOREIGN KEY(device_id) REFERENCES agent_devices(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_diagnostics (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  report_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(device_id) REFERENCES agent_devices(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_enrollment_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  desired_device_id TEXT,
  display_name TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT 'android',
  code_salt TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  used_device_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(used_device_id) REFERENCES agent_devices(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS transport_configs (
  server_id TEXT PRIMARY KEY,
  transport_type TEXT NOT NULL,
  interface_name TEXT NOT NULL,
  config_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_probe_jobs (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT '',
  candidate_server_ids TEXT NOT NULL,
  url TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  assigned_device_id TEXT NOT NULL DEFAULT '',
  claimed_by_device_id TEXT NOT NULL DEFAULT '',
  apply_cache INTEGER NOT NULL DEFAULT 1,
  connect_timeout INTEGER NOT NULL DEFAULT 5,
  max_time INTEGER NOT NULL DEFAULT 12,
  priority INTEGER NOT NULL DEFAULT 100,
  attempts INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}',
  winner_server_id TEXT,
  score_ms INTEGER,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(winner_server_id) REFERENCES servers(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS worker_status (
  name TEXT PRIMARY KEY,
  status_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_aliases (
  alias TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  targets_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_service_aliases (
  user_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  label TEXT NOT NULL,
  targets_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, alias),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS critical_services (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT '',
  service_key TEXT NOT NULL,
  label TEXT NOT NULL,
  targets_json TEXT NOT NULL,
  success_pattern TEXT NOT NULL DEFAULT '',
  failure_pattern TEXT NOT NULL DEFAULT '',
  routing_enabled INTEGER NOT NULL DEFAULT 0,
  candidate_server_ids TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, service_key)
);

CREATE TABLE IF NOT EXISTS domain_discovery_queue (
  domain TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'pending',
  source TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 1,
  user_ids_json TEXT NOT NULL DEFAULT '[]',
  client_ips_json TEXT NOT NULL DEFAULT '[]',
  note TEXT NOT NULL DEFAULT ''
);
"""


LOGIN_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cudy VPN Login</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #172033;
      --muted: #647084;
      --line: #d9dee8;
      --accent: #1769e0;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 20px;
    }
    main {
      width: min(420px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
    }
    h1 { font-size: 20px; margin: 0 0 18px; }
    label { display: block; margin: 12px 0 6px; color: var(--muted); }
    input {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
    }
    button {
      width: 100%;
      min-height: 38px;
      margin-top: 16px;
      border: 1px solid #145bbf;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
    }
    .status { min-height: 20px; margin-top: 12px; color: var(--muted); }
    .error { color: var(--danger); }
  </style>
</head>
<body>
  <main>
    <h1>Cudy VPN Login</h1>
    <form id="loginForm">
      <label for="username">User</label>
      <input id="username" name="username" autocomplete="username" required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
    <p id="status" class="status"></p>
  </main>
  <script>
    const nextUrl = new URLSearchParams(location.search).get("next") || "/";
    document.getElementById("loginForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("status");
      status.className = "status";
      try {
        const response = await fetch("/api/login", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            username: document.getElementById("username").value,
            password: document.getElementById("password").value
          })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || response.statusText);
        location.href = nextUrl;
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
  </script>
</body>
</html>
"""


USER_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cudy VPN</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #172033;
      --muted: #647084;
      --line: #d9dee8;
      --accent: #1769e0;
      --danger: #b42318;
      --ok: #147a42;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { font-size: 20px; margin: 0; }
    main { max-width: 1120px; min-width: 0; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section {
      min-width: 0;
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }
    h2 { font-size: 16px; margin: 0 0 14px; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    select, input {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 10px;
      background: #fff;
      color: var(--text);
    }
    input[type="text"] { min-width: min(340px, 100%); }
    button {
      min-height: 36px;
      border: 1px solid #145bbf;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 7px 12px;
      cursor: pointer;
    }
    button.secondary { background: #fff; color: var(--accent); border-color: var(--line); }
    button.danger { background: #fff; color: var(--danger); border-color: #efc0ba; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 9px 8px; text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-weight: 600; }
    .muted { color: var(--muted); }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      background: #eef3fb;
      color: #24446f;
      font-size: 12px;
    }
    .status { min-height: 20px; color: var(--muted); }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    .priority-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      max-width: 100%;
    }
    .priority-list select { min-width: 180px; }
    .winner-list { margin-top: 6px; font-size: 12px; line-height: 1.45; max-width: 720px; }
    .priority-list[hidden], .field[hidden] { display: none; }
    @media (max-width: 720px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 14px; }
      table, thead, tbody, tr, th, td { display: block; }
      thead { display: none; }
      tr { border-bottom: 1px solid var(--line); padding: 8px 0; }
      td { border: 0; padding: 5px 0; }
      td::before { content: attr(data-label); display: block; color: var(--muted); font-size: 12px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Cudy VPN</h1>
    <div class="row">
      <a href="/admin">Admin</a>
      <button id="logoutButton" class="secondary" type="button">Logout</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Default Route</h2>
      <div class="row">
        <select id="defaultServer"></select>
        <input id="defaultPriority" type="text" placeholder="Auto priority: proxyde, proxynl, all-rest" autocomplete="off">
        <button id="saveDefault">Save</button>
      </div>
      <p id="defaultStatus" class="status"></p>
    </section>

    <section>
      <h2>Domain Routes</h2>
      <form id="routeForm" class="row">
        <input id="domainInput" type="text" placeholder="example.com" autocomplete="off">
        <select id="routeServer"></select>
        <input id="routePriority" type="text" placeholder="Auto priority: proxyde, proxynl, all-rest" autocomplete="off">
        <button type="submit">Add</button>
      </form>
      <p id="routeStatus" class="status"></p>
      <table>
        <thead><tr><th>Domain</th><th>Server</th><th>Provider</th><th>Priority</th><th></th></tr></thead>
        <tbody id="routesBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Route Lookup</h2>
      <form id="lookupForm" class="row">
        <input id="lookupTarget" type="text" placeholder="telegram, youtube, example.com, 1.1.1.1" autocomplete="off">
        <button type="submit">Check</button>
      </form>
      <p id="lookupStatus" class="status"></p>
      <table>
        <thead><tr><th>Target</th><th>State</th><th>Server</th><th>Rule</th><th>Auto</th><th>Notes</th></tr></thead>
        <tbody id="lookupBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Lookup Aliases</h2>
      <p id="aliasHelp" class="muted">Local aliases override global lookup shortcuts with the same name. They do not create tunnel rules.</p>
      <form id="aliasForm" class="row">
        <input id="aliasInput" type="text" placeholder="alias, e.g. телеграм" autocomplete="off">
        <input id="aliasLabel" type="text" placeholder="label" autocomplete="off">
        <input id="aliasTargets" type="text" placeholder="lookup targets: domain, IP/CIDR, ..." autocomplete="off">
        <button type="submit">Save</button>
      </form>
      <p id="aliasStatus" class="status"></p>
      <table>
        <thead><tr><th>Alias</th><th>Label</th><th>Scope</th><th>Lookup Targets</th><th>Routing Effect</th><th></th></tr></thead>
        <tbody id="aliasesBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Important Services</h2>
      <p class="muted">The agent checks these services. A local entry with the same key replaces the global check.</p>
      <form id="criticalServiceForm" class="row">
        <input id="criticalServiceKey" type="text" placeholder="key, e.g. chatgpt" autocomplete="off">
        <input id="criticalServiceLabel" type="text" placeholder="label" autocomplete="off">
        <input id="criticalServiceTargets" type="text" placeholder="URLs, separated by commas" autocomplete="off">
        <input id="criticalServiceSuccess" type="text" placeholder="success regex (optional)" autocomplete="off">
        <input id="criticalServiceFailure" type="text" placeholder="failure regex (optional)" autocomplete="off">
        <input id="criticalServiceCandidates" type="text" placeholder="Auto priority: proxyde, proxynl, all-rest" autocomplete="off">
        <label class="inline muted"><input id="criticalServiceRouting" type="checkbox"> Route as one Auto group</label>
        <label class="inline muted"><input id="criticalServiceEnabled" type="checkbox" checked> Enabled</label>
        <button type="submit">Save</button>
      </form>
      <p id="criticalServiceStatus" class="status"></p>
      <table>
        <thead><tr><th>Service</th><th>Scope</th><th>Targets</th><th>Content checks</th><th>Routing</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="criticalServicesBody"></tbody>
      </table>
    </section>
  </main>
  <script>
    const state = { servers: [], routes: [], user: null, aliases: [], criticalServices: { global: [], local: [], effective: [] } };
    const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
    const serverLabel = id => (id === "all-rest" ? "All rest" : (state.servers.find(s => s.id === id) || { label: id }).label);
    const serverProvider = id => (state.servers.find(s => s.id === id) || { provider: "" }).provider || "";
    const priorityText = policy => policy ? (policy.candidate_server_ids || []).join(", ") : "";
    const optionLabel = s => `${s.label}${s.candidate_available === false ? " (stale)" : ""}`;

    async function api(path, options) {
      const response = await fetch(path, {
        headers: { "content-type": "application/json" },
        ...options
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function fillServerSelect(select, value) {
      select.innerHTML = state.servers.map(s => {
        const geo = s.geo_region ? `${s.geo_country}-${s.geo_region}` : s.geo_country;
        return `<option value="${s.id}" ${s.candidate_available === false ? "disabled" : ""}>${optionLabel(s)} ${geo ? "(" + geo + ")" : ""}</option>`;
      }).join("");
      select.value = value || "auto";
    }

    function renderRoutes() {
      const body = document.getElementById("routesBody");
      if (!state.routes.length) {
        body.innerHTML = '<tr><td data-label="Domain" colspan="5" class="muted">No domain routes yet.</td></tr>';
        return;
      }
      body.innerHTML = state.routes.map(route => `
        <tr>
          <td data-label="Domain">${route.domain}</td>
          <td data-label="Server"><span class="pill">${serverLabel(route.server_id)}</span></td>
          <td data-label="Provider">${serverProvider(route.server_id)}</td>
          <td data-label="Priority">${priorityText(route.auto_candidate_policy)}</td>
          <td><button class="danger" data-delete="${route.domain}">Delete</button></td>
        </tr>
      `).join("");
      body.querySelectorAll("[data-delete]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/domain-routes?domain=${encodeURIComponent(button.dataset.delete)}`, { method: "DELETE" });
          await load();
        });
      });
    }

    function renderLookup(result) {
      const body = document.getElementById("lookupBody");
      body.innerHTML = (result.results || []).map(item => `
        <tr>
          <td data-label="Target">${item.target}</td>
          <td data-label="State">${item.route_state}</td>
          <td data-label="Server">${item.server_id === "direct" ? "direct" : serverLabel(item.server_id)}</td>
          <td data-label="Rule">${item.matched_rule ? `${item.matched_rule.source}:${item.matched_rule.target_cidr || item.matched_rule.domain}` : "-"}</td>
          <td data-label="Auto">${item.auto_cache ? `${item.auto_cache.selected_server_id || "-"} ${item.auto_cache.score_ms ?? ""}` : (item.auto_candidate_policy ? (item.auto_candidate_policy.candidate_server_ids || []).join(" -> ") : "-")}</td>
          <td data-label="Notes">${(item.warnings || []).join("; ")}</td>
        </tr>
      `).join("") || '<tr><td colspan="6" class="muted">No result.</td></tr>';
    }

    function renderAliases() {
      const body = document.getElementById("aliasesBody");
      body.innerHTML = state.aliases.length ? state.aliases.map(item => `
        <tr>
          <td data-label="Alias">${escapeHtml(item.alias)}</td>
          <td data-label="Label">${escapeHtml(item.label)}</td>
          <td data-label="Scope">${item.scope === "user" ? "Local" : "Global"}</td>
          <td data-label="Lookup Targets">${(item.targets || []).map(escapeHtml).join(", ")}</td>
          <td data-label="Routing Effect" class="muted">none; use Route Lookup or Domain Routes</td>
          <td>${item.scope === "user" ? `<button class="danger" data-delete-alias="${escapeHtml(item.alias)}">Delete local</button>` : ""}</td>
        </tr>
      `).join("") : '<tr><td data-label="Alias" colspan="6" class="muted">No aliases.</td></tr>';
      body.querySelectorAll("[data-delete-alias]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/user/service-aliases?alias=${encodeURIComponent(button.dataset.deleteAlias)}`, { method: "DELETE" });
          await load();
        });
      });
    }

    function renderCriticalServices() {
      const body = document.getElementById("criticalServicesBody");
      const locals = new Map((state.criticalServices.local || []).map(item => [item.service_key, item]));
      const visible = [
        ...(state.criticalServices.global || []).filter(item => !locals.has(item.service_key)),
        ...(state.criticalServices.local || [])
      ];
      body.innerHTML = visible.length ? visible.map(item => `
        <tr>
          <td data-label="Service"><strong>${escapeHtml(item.label)}</strong><br><span class="muted">${escapeHtml(item.service_key)}</span></td>
          <td data-label="Scope">${item.scope === "global" ? "Global" : "Local"}</td>
          <td data-label="Targets">${(item.targets || []).map(escapeHtml).join("<br>")}</td>
          <td data-label="Content checks"><span class="muted">success:</span> ${escapeHtml(item.success_pattern || "-")}<br><span class="muted">failure:</span> ${escapeHtml(item.failure_pattern || "-")}</td>
          <td data-label="Routing">${item.routing_enabled ? `One Auto winner<br><span class="muted">${(item.candidate_server_ids || []).map(escapeHtml).join(" -> ")}</span>` : "health only"}</td>
          <td data-label="Enabled">${item.enabled ? "yes" : "no"}</td>
          <td>${item.scope === "user" ? `<button class="danger" data-delete-critical="${escapeHtml(item.service_key)}">Delete local</button>` : ""}</td>
        </tr>
      `).join("") : '<tr><td colspan="7" class="muted">No important services configured.</td></tr>';
      body.querySelectorAll("[data-delete-critical]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/critical-services?service_key=${encodeURIComponent(button.dataset.deleteCritical)}`, { method: "DELETE" });
          await load();
        });
      });
    }

    async function load() {
      const data = await api("/api/bootstrap");
      state.servers = data.servers;
      state.routes = data.routes;
      state.user = data.user;
      state.aliases = data.aliases || [];
      state.criticalServices = data.critical_services || { global: [], local: [], effective: [] };
      document.getElementById("aliasHelp").textContent = "Create local lookup aliases here. A local alias replaces the global alias with the same name for your account only; aliases do not create tunnel rules.";
      state.default_auto_candidate_policy = data.default_auto_candidate_policy || null;
      fillServerSelect(document.getElementById("defaultServer"), state.user.default_server_id);
      fillServerSelect(document.getElementById("routeServer"), "auto");
      document.getElementById("defaultPriority").value = priorityText(state.default_auto_candidate_policy);
      renderRoutes();
      renderAliases();
      renderCriticalServices();
      togglePriorityFields();
    }

    function togglePriorityFields() {
      document.getElementById("defaultPriority").hidden = document.getElementById("defaultServer").value !== "auto";
      document.getElementById("routePriority").hidden = document.getElementById("routeServer").value !== "auto";
    }

    document.getElementById("logoutButton").addEventListener("click", async () => {
      await api("/api/logout", { method: "POST", body: "{}" });
      location.href = "/login";
    });

    document.getElementById("saveDefault").addEventListener("click", async () => {
      const status = document.getElementById("defaultStatus");
      status.className = "status";
      try {
        await api("/api/user/default-server", {
          method: "POST",
          body: JSON.stringify({
            server_id: document.getElementById("defaultServer").value,
            auto_candidate_server_ids: document.getElementById("defaultPriority").value
          })
        });
        status.textContent = "Saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });

    document.getElementById("routeForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("routeStatus");
      status.className = "status";
      try {
        await api("/api/domain-routes", {
          method: "POST",
          body: JSON.stringify({
            domain: document.getElementById("domainInput").value,
            server_id: document.getElementById("routeServer").value,
            auto_candidate_server_ids: document.getElementById("routePriority").value
          })
        });
        document.getElementById("domainInput").value = "";
        document.getElementById("routePriority").value = "";
        status.textContent = "Saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });

    document.getElementById("lookupForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("lookupStatus");
      status.className = "status";
      try {
        const result = await api(`/api/route-lookup?target=${encodeURIComponent(document.getElementById("lookupTarget").value)}`);
        renderLookup(result);
        status.textContent = result.alias ? `Alias ${result.alias.label}: ${result.alias.targets.length} target(s).` : "Lookup complete.";
        status.className = "status ok";
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });

    document.getElementById("aliasForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("aliasStatus");
      status.className = "status";
      try {
        await api("/api/user/service-aliases", {
          method: "POST",
          body: JSON.stringify({
            alias: document.getElementById("aliasInput").value,
            label: document.getElementById("aliasLabel").value,
            targets: document.getElementById("aliasTargets").value
          })
        });
        event.target.reset();
        status.textContent = "Local alias saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });

    document.getElementById("criticalServiceForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("criticalServiceStatus");
      status.className = "status";
      try {
        await api("/api/critical-services", {
          method: "POST",
          body: JSON.stringify({
            service_key: document.getElementById("criticalServiceKey").value,
            label: document.getElementById("criticalServiceLabel").value,
            targets: document.getElementById("criticalServiceTargets").value,
            success_pattern: document.getElementById("criticalServiceSuccess").value,
            failure_pattern: document.getElementById("criticalServiceFailure").value,
            routing_enabled: document.getElementById("criticalServiceRouting").checked,
            candidate_server_ids: document.getElementById("criticalServiceCandidates").value,
            enabled: document.getElementById("criticalServiceEnabled").checked
          })
        });
        event.target.reset();
        document.getElementById("criticalServiceRouting").checked = false;
        document.getElementById("criticalServiceEnabled").checked = true;
        status.textContent = "Saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });

    document.getElementById("defaultServer").addEventListener("change", togglePriorityFields);
    document.getElementById("routeServer").addEventListener("change", togglePriorityFields);

    load().catch(error => {
      document.getElementById("routeStatus").textContent = error.message;
      document.getElementById("routeStatus").className = "status error";
    });
  </script>
</body>
</html>
"""


ADMIN_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cudy VPN Admin</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #172033;
      --muted: #647084;
      --line: #d9dee8;
      --accent: #1769e0;
      --danger: #b42318;
      --ok: #147a42;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { font-size: 20px; margin: 0; }
    main { max-width: 1280px; min-width: 0; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section { min-width: 0; overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
    h2 { font-size: 16px; margin: 0 0 14px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-weight: 600; }
    input[type="text"], input[type="password"], input[type="search"], select {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 9px;
      min-width: 120px;
    }
    button {
      min-height: 34px;
      border: 1px solid #145bbf;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 6px 10px;
      cursor: pointer;
    }
    .muted { color: var(--muted); }
    .status { min-height: 20px; color: var(--muted); }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    .inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .toolbar { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; margin-bottom: 14px; }
    .field { display: grid; gap: 4px; }
    .field label { color: var(--muted); font-size: 12px; }
    button.secondary { background: #fff; color: var(--accent); border-color: var(--line); }
    button.danger { background: #fff; color: var(--danger); border-color: #efc0ba; }
    a.button { display: inline-flex; align-items: center; min-height: 34px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--accent); text-decoration: none; }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 10px 0 14px; }
    .summary-item { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fbfcfe; }
    .summary-label { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .summary-value { font-size: 18px; font-weight: 650; }
    .badge { display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 8px; font-size: 12px; border: 1px solid var(--line); color: var(--muted); }
    .badge.ok { color: var(--ok); border-color: #b9dfc8; background: #f0fbf4; }
    .badge.error { color: var(--danger); border-color: #efc0ba; background: #fff6f4; }
    .admin-tabs { max-width: 1280px; margin: 0 auto; padding: 14px 24px 0; display: flex; gap: 6px; overflow-x: auto; }
    .admin-tab { white-space: nowrap; background: #fff; color: var(--muted); border-color: var(--line); }
    .admin-tab[aria-selected="true"] { color: #fff; background: var(--accent); border-color: var(--accent); }
    section[data-admin-section][hidden] { display: none; }
    @media (max-width: 720px) {
      .admin-tabs { padding: 10px 14px 0; }
      main { padding: 14px; }
      .summary-grid { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Cudy VPN Admin</h1>
    <div class="inline">
      <a href="/">User</a>
      <button id="logoutButton" type="button">Logout</button>
    </div>
  </header>
  <nav id="adminTabs" class="admin-tabs" aria-label="Admin sections"></nav>
  <main>
    <section id="admin-status" data-admin-section="status" data-admin-label="Status">
      <div class="inline">
        <h2>System Status</h2>
        <button id="refreshSystemStatus" class="secondary" type="button">Refresh</button>
      </div>
      <p id="systemStatusText" class="status"></p>
      <div id="systemStatusGrid" class="summary-grid"></div>
      <table>
        <thead><tr><th>Area</th><th>Status</th><th>Age</th><th>Details</th></tr></thead>
        <tbody id="systemStatusBody"></tbody>
      </table>
    </section>
    <section id="admin-servers" data-admin-section="servers" data-admin-label="Servers">
      <h2>Servers</h2>
      <p id="serverStatus" class="status"></p>
      <table>
        <thead>
          <tr><th>ID</th><th>Label</th><th>Provider</th><th>Interface</th><th>Geo</th><th>Status</th><th>Enabled</th><th>User</th><th></th></tr>
        </thead>
        <tbody id="serversBody"></tbody>
      </table>
    </section>
    <section id="admin-users" data-admin-section="users" data-admin-label="Users">
      <h2>Users</h2>
      <form id="newUserForm" class="toolbar">
        <div class="field"><label>ID</label><input id="newUserId" type="text" autocomplete="off"></div>
        <div class="field"><label>Name</label><input id="newUserName" type="text" autocomplete="off"></div>
        <div class="field"><label>Role</label><select id="newUserRole"><option value="user">user</option><option value="admin">admin</option></select></div>
        <div class="field"><label>Client IP</label><input id="newUserClientIp" type="text" placeholder="10.77.0.x" autocomplete="off"></div>
        <label class="inline muted"><input id="newUserCreateCudy" type="checkbox"> Create legacy Cudy peer</label>
        <div class="field">
          <label>Password</label>
          <div class="inline">
            <input id="newUserPassword" type="password" autocomplete="new-password" placeholder="new password">
            <button class="secondary" type="button" data-toggle-password="newUserPassword" title="Show/hide the password typed here. Stored passwords cannot be viewed.">Show typed</button>
          </div>
        </div>
        <button type="submit">Create</button>
        <button id="syncCudyClients" class="secondary" type="button">Sync Cudy</button>
      </form>
      <p id="userStatus" class="status"></p>
      <div class="toolbar">
        <div class="field"><label>Find user</label><input id="userFilter" type="search" placeholder="ID, name, IP or role" autocomplete="off"></div>
        <span id="userFilterStatus" class="muted"></span>
      </div>
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Role</th><th>Client IP</th><th>Default</th><th>Enabled</th><th>Web login</th><th>Password</th><th>Actions</th></tr></thead>
        <tbody id="usersBody"></tbody>
      </table>
    </section>
    <section id="admin-lookup" data-admin-section="lookup" data-admin-label="Lookup">
      <h2>Route Lookup</h2>
      <form id="adminLookupForm" class="toolbar">
        <div class="field"><label>User</label><select id="lookupUser"></select></div>
        <div class="field"><label>IP / URL / Alias</label><input id="adminLookupTarget" type="text" placeholder="telegram, youtube, example.com, 1.1.1.1" autocomplete="off"></div>
        <button type="submit">Check Route</button>
      </form>
      <p id="adminLookupStatus" class="status"></p>
      <table>
        <thead><tr><th>Target</th><th>State</th><th>Server</th><th>Rule</th><th>Auto</th><th>Notes</th></tr></thead>
        <tbody id="adminLookupBody"></tbody>
      </table>
      <h3>Lookup Aliases</h3>
      <p class="muted">Aliases only expand names in Route Lookup, for example gemini -> gemini.google.com. They are not tunnel rules. Put domains into Global Domain Routes or per-user Domain Routes to route them.</p>
      <form id="adminAliasForm" class="toolbar">
        <div class="field"><label>Alias</label><input id="adminAliasInput" type="text" placeholder="телеграм" autocomplete="off"></div>
        <div class="field"><label>Label</label><input id="adminAliasLabel" type="text" placeholder="Telegram" autocomplete="off"></div>
        <div class="field"><label>Lookup Targets</label><input id="adminAliasTargets" type="text" placeholder="domain, IP/CIDR, ..." autocomplete="off"></div>
        <button type="submit">Save Alias</button>
      </form>
      <p id="adminAliasStatus" class="status"></p>
      <table>
        <thead><tr><th>Alias</th><th>Label</th><th>Lookup Targets</th><th>Routing Effect</th><th></th></tr></thead>
        <tbody id="adminAliasesBody"></tbody>
      </table>
      <h3>Important Services</h3>
      <p class="muted">Global checks apply to every agent. A user entry with the same key replaces the global check; a disabled user entry excludes it.</p>
      <form id="adminCriticalServiceForm" class="toolbar">
        <div class="field"><label>Scope</label><select id="adminCriticalServiceUser"><option value="">Global</option></select></div>
        <div class="field"><label>Key</label><input id="adminCriticalServiceKey" type="text" placeholder="chatgpt" autocomplete="off"></div>
        <div class="field"><label>Label</label><input id="adminCriticalServiceLabel" type="text" placeholder="ChatGPT" autocomplete="off"></div>
        <div class="field"><label>URLs</label><input id="adminCriticalServiceTargets" type="text" placeholder="https://example.com/" autocomplete="off"></div>
        <div class="field"><label>Success regex</label><input id="adminCriticalServiceSuccess" type="text" autocomplete="off"></div>
        <div class="field"><label>Failure regex</label><input id="adminCriticalServiceFailure" type="text" autocomplete="off"></div>
        <div class="field"><label>Auto priority</label><input id="adminCriticalServiceCandidates" type="text" placeholder="proxyde, proxynl, all-rest" autocomplete="off"></div>
        <label class="inline muted"><input id="adminCriticalServiceRouting" type="checkbox"> Route as one Auto group</label>
        <label class="inline muted"><input id="adminCriticalServiceEnabled" type="checkbox" checked> Enabled</label>
        <button type="submit">Save Service</button>
      </form>
      <p id="adminCriticalServiceStatus" class="status"></p>
      <table>
        <thead><tr><th>Service</th><th>Scope</th><th>URLs</th><th>Content checks</th><th>Routing</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="adminCriticalServicesBody"></tbody>
      </table>
    </section>
    <section id="admin-discovery" data-admin-section="discovery" data-admin-label="Discovery">
      <h2>Domain Discovery</h2>
      <div class="toolbar">
        <button id="refreshDomainDiscovery" class="secondary" type="button">Refresh</button>
        <div class="field"><label>Status</label><select id="domainDiscoveryStatusFilter"><option value="">All</option><option value="pending">pending</option><option value="reviewed">reviewed</option><option value="ignored">ignored</option><option value="promoted">promoted</option></select></div>
      </div>
      <p id="domainDiscoveryStatus" class="status"></p>
      <table>
        <thead><tr><th>Domain</th><th>Status</th><th>Hits</th><th>Users</th><th>Client IPs</th><th>Last Seen</th><th>Note</th><th>Actions</th></tr></thead>
        <tbody id="domainDiscoveryBody"></tbody>
      </table>
    </section>
    <section id="admin-global-routes" data-admin-section="global-routes" data-admin-label="Global Routes">
      <h2>Global Domain Routes</h2>
      <p class="muted">This is the global tunnel list. Server Auto means the domain is tunneled through the current Auto winner or the first available candidate until probes pick a better winner.</p>
      <form id="globalDefaultPriorityForm" class="toolbar">
        <div id="globalDefaultAutoField" class="field">
          <label>Default Priority</label>
          <div id="globalDefaultAutoList" class="priority-list"></div>
          <input id="globalDefaultAutoText" type="text" placeholder="proxyde, uswest, all-rest" autocomplete="off">
        </div>
        <button type="submit">Save Default</button>
      </form>
      <p id="globalDefaultPriorityStatus" class="status"></p>
      <form id="globalRouteForm" class="toolbar">
        <div class="field"><label>Domain</label><input id="globalRouteDomain" type="text" placeholder="example.com" autocomplete="off"></div>
        <div class="field"><label>Server</label><select id="globalRouteServer"></select></div>
        <div id="globalRouteAutoField" class="field">
          <label>Priority</label>
          <div id="globalRouteAutoList" class="priority-list"></div>
          <input id="globalRouteAutoText" type="text" placeholder="proxyde, uswest, all-rest" autocomplete="off">
          <div id="globalRouteAutoWinners" class="winner-list muted"></div>
        </div>
        <button type="submit">Save Global</button>
      </form>
      <p id="globalRouteStatus" class="status"></p>
      <table>
        <thead><tr><th>Domain</th><th>Server</th><th>Priority</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="globalRoutesBody"></tbody>
      </table>
    </section>
    <section id="admin-user-routes" data-admin-section="user-routes" data-admin-label="User Routes">
      <h2>Domain Routes</h2>
      <p class="muted">These per-user routes override global domain routes. Use Auto with an ordered priority list for user-specific candidate preference.</p>
      <form id="userDefaultPriorityForm" class="toolbar">
        <div class="field"><label>User</label><select id="defaultRouteUser"></select></div>
        <div id="userDefaultAutoField" class="field">
          <label>Default Priority</label>
          <div id="userDefaultAutoList" class="priority-list"></div>
          <input id="userDefaultAutoText" type="text" placeholder="proxyde, uswest, all-rest" autocomplete="off">
        </div>
        <button type="submit">Save User Default</button>
      </form>
      <p id="userDefaultPriorityStatus" class="status"></p>
      <form id="adminRouteForm" class="toolbar">
        <div class="field"><label>User</label><select id="routeUser"></select></div>
        <div class="field"><label>Domain</label><input id="adminRouteDomain" type="text" placeholder="example.com" autocomplete="off"></div>
        <div class="field"><label>Server</label><select id="adminRouteServer"></select></div>
        <div id="adminRouteAutoField" class="field">
          <label>Priority</label>
          <div id="adminRouteAutoList" class="priority-list"></div>
          <input id="adminRouteAutoText" type="text" placeholder="proxyde, uswest, all-rest" autocomplete="off">
          <div id="adminRouteAutoWinners" class="winner-list muted"></div>
        </div>
        <button type="submit">Save Route</button>
      </form>
      <p id="adminRouteStatus" class="status"></p>
      <table>
        <thead><tr><th>User</th><th>Domain</th><th>Server</th><th>Priority</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="routesBody"></tbody>
      </table>
    </section>
    <section id="admin-auto-cache" data-admin-section="auto-cache" data-admin-label="Auto Cache">
      <h2>Auto Cache</h2>
      <form id="autoCacheForm" class="toolbar">
        <div class="field"><label>Domain</label><input id="autoCacheDomain" type="text" placeholder="example.com" autocomplete="off"></div>
        <div class="field"><label>Selected Server</label><select id="autoCacheServer"></select></div>
        <div class="field"><label>Score ms</label><input id="autoCacheScore" type="number" min="0" step="1" placeholder="optional"></div>
        <div class="field"><label>Status</label><input id="autoCacheStatusInput" type="text" value="manual" autocomplete="off"></div>
        <button type="submit">Save Auto</button>
      </form>
      <p id="autoCacheStatus" class="status"></p>
      <form id="autoSelectForm" class="toolbar">
        <div class="field"><label>Probe Domain</label><input id="autoSelectDomain" type="text" placeholder="example.com" autocomplete="off"></div>
        <div class="field"><label>User</label><select id="autoSelectUser"></select></div>
        <div class="field"><label>Candidates</label><input id="autoSelectCandidates" type="text" placeholder="blank = policy"></div>
        <button type="submit">Run Auto</button>
        <label class="inline muted"><input id="autoSelectApply" type="checkbox" checked> Save</label>
        <label class="inline muted"><input id="autoSelectDeploy" type="checkbox"> Deploy</label>
        <label class="inline muted"><input id="autoSelectProfiles" type="checkbox"> LokVPN profiles</label>
      </form>
      <p id="autoSelectStatus" class="status"></p>
      <table>
        <thead><tr><th>Domain</th><th>Selected Server</th><th>Score</th><th>Status</th><th>Checked</th><th></th></tr></thead>
        <tbody id="autoCacheBody"></tbody>
      </table>
    </section>
    <section id="admin-agents" data-admin-section="agents" data-admin-label="Agents">
      <h2>Auto Probe Jobs</h2>
      <div class="toolbar">
        <button id="runAutoWorker" type="button">Run Worker Once</button>
        <button id="refreshAutoJobs" class="secondary" type="button">Refresh</button>
        <div class="field"><label>Max Jobs</label><input id="autoWorkerMaxJobs" type="number" min="1" max="50" step="1" value="5"></div>
        <div class="field"><label>Max Candidates</label><input id="autoWorkerMaxCandidates" type="number" min="1" max="50" step="1" value="4"></div>
        <div class="field"><label>Cache TTL sec</label><input id="autoWorkerCacheTtl" type="number" min="0" step="60" value="3600"></div>
      </div>
      <p id="autoProbeStatus" class="status"></p>
      <table>
        <thead><tr><th>Status</th><th>Domain</th><th>Candidates</th><th>Assigned</th><th>Claimed</th><th>Winner</th><th>Score</th><th>Updated</th></tr></thead>
        <tbody id="autoProbeJobsBody"></tbody>
      </table>
      <h3>Agents</h3>
      <form id="enrollmentForm" class="toolbar">
        <div class="field"><label>User</label><select id="enrollmentUser"></select></div>
        <div class="field"><label>Platform</label><select id="enrollmentPlatform"><option value="android">Android</option><option value="windows">Windows</option><option value="linux">Linux</option><option value="macos">macOS</option><option value="other">Other</option></select></div>
        <div class="field"><label>Device ID</label><input id="enrollmentDeviceId" type="text" placeholder="user-android"></div>
        <div class="field"><label>Name</label><input id="enrollmentDisplayName" type="text" placeholder="Android phone"></div>
        <div class="field"><label>TTL hours</label><input id="enrollmentTtlHours" type="number" min="1" max="720" step="1" value="24"></div>
        <button type="submit">Create one-time code</button>
      </form>
      <p id="enrollmentStatus" class="status"></p>
      <table>
        <thead><tr><th>Code ID</th><th>User</th><th>Device</th><th>Platform</th><th>State</th><th>Expires</th><th>Updated</th><th></th></tr></thead>
        <tbody id="enrollmentCodesBody"></tbody>
      </table>
      <h3>Agent Updates</h3>
      <table>
        <thead><tr><th>Platform</th><th>Version</th><th>Code</th><th>Package</th><th>SHA256</th><th>Notes</th></tr></thead>
        <tbody id="agentUpdatesBody"></tbody>
      </table>
      <div class="toolbar">
        <div class="field"><label>Find device</label><input id="agentFilter" type="search" placeholder="Device, user or platform" autocomplete="off"></div>
        <span id="agentFilterStatus" class="muted"></span>
      </div>
      <table>
        <thead><tr><th>Device</th><th>User</th><th>Platform</th><th>Enabled</th><th>Last Seen</th><th>Reported</th><th>Health</th><th>Applied</th><th>Errors</th><th></th></tr></thead>
        <tbody id="agentStatusBody"></tbody>
      </table>
      <h3>Diagnostics</h3>
      <table>
        <thead><tr><th>Created</th><th>Device</th><th>User</th><th>Platform</th><th>Summary</th><th></th></tr></thead>
        <tbody id="agentDiagnosticsBody"></tbody>
      </table>
      <pre id="agentDiagnosticReport" class="muted"></pre>
    </section>
    <section id="admin-transports" data-admin-section="transports" data-admin-label="Transports">
      <h2>Provider Transports</h2>
      <div class="toolbar">
        <div class="field"><label>Provider</label><select id="providerRefreshProvider"><option value="all">All</option><option value="vpntype">VPNtype</option><option value="lokvpn">LokVPN</option></select></div>
        <div class="field"><label>Servers</label><input id="providerRefreshServers" type="text" placeholder="blank = all, or proxyde,lokvpn-de1"></div>
        <button id="runProviderRefresh" type="button">Refresh Provider</button>
        <button id="refreshProviderTransports" class="secondary" type="button">Refresh Table</button>
        <label class="inline muted"><input id="providerRefreshSkipVerify" type="checkbox"> Skip Verify</label>
      </div>
      <p id="providerRefreshStatus" class="status"></p>
      <table>
        <thead><tr><th>Server</th><th>Provider</th><th>Type</th><th>Interface</th><th>Endpoint</th><th>Source</th><th>Version</th><th>Updated</th><th>Enabled</th></tr></thead>
        <tbody id="providerTransportsBody"></tbody>
      </table>
    </section>
    <section id="admin-deploy" data-admin-section="deploy" data-admin-label="Deploy">
      <h2>Deploy Preview</h2>
      <div class="toolbar">
        <button id="refreshPlan" type="button">Refresh Route Plan</button>
        <button id="refreshDeployPlan" class="secondary" type="button">Refresh Deploy Plan</button>
        <button id="applyDeployPlan" class="danger" type="button">Apply Routes</button>
        <label class="inline muted"><input id="applyInstallScripts" type="checkbox"> Install scripts</label>
      </div>
      <p id="planStatus" class="status"></p>
      <pre id="routePlan" class="muted"></pre>
      <pre id="deployPlan" class="muted"></pre>
    </section>
  </main>
  <script>
    const state = { servers: [], users: [], routes: [], globalRoutes: [], autoCache: [], autoCandidates: [], probeJobs: [], agentStatus: [], agentDiagnostics: [], enrollmentCodes: [], agentUpdates: [], transportConfigs: [], serviceAliases: [], criticalServices: [], domainDiscovery: [], systemStatus: null };
    const ALL_REST = "__all_rest__";
    const autoEditors = { globalDefault: [], userDefault: [], globalRoute: [], adminRoute: [] };
    const adminSections = Array.from(document.querySelectorAll("[data-admin-section]"));
    function activateAdminSection(name, updateHash = true) {
      const selected = adminSections.some(section => section.dataset.adminSection === name) ? name : "status";
      adminSections.forEach(section => { section.hidden = section.dataset.adminSection !== selected; });
      document.querySelectorAll(".admin-tab").forEach(button => {
        button.setAttribute("aria-selected", button.dataset.adminTab === selected ? "true" : "false");
      });
      if (updateHash && location.hash !== `#${selected}`) history.replaceState(null, "", `#${selected}`);
    }
    function initializeAdminTabs() {
      const tabs = document.getElementById("adminTabs");
      tabs.innerHTML = adminSections.map(section => `
        <button class="admin-tab" type="button" data-admin-tab="${section.dataset.adminSection}" aria-controls="${section.id}" aria-selected="false">${section.dataset.adminLabel}</button>
      `).join("");
      tabs.querySelectorAll("[data-admin-tab]").forEach(button => {
        button.addEventListener("click", () => activateAdminSection(button.dataset.adminTab));
      });
      activateAdminSection(location.hash.replace(/^#/, "") || "status", false);
    }
    initializeAdminTabs();
    window.addEventListener("hashchange", () => activateAdminSection(location.hash.replace(/^#/, ""), false));
    const serverLabel = id => (id === ALL_REST || id === "all-rest" ? "All rest" : (state.servers.find(s => s.id === id) || { label: id }).label);
    const serverOptionLabel = s => `${s.label}${s.candidate_available === false ? " (stale)" : ""}`;
    async function api(path, options) {
      const response = await fetch(path, {
        headers: { "content-type": "application/json" },
        ...options
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }
    function serverOptions(value) {
      return state.servers
        .filter(s => s.candidate_available !== false || s.id === value)
        .map(s => `<option value="${s.id}" ${s.id === value ? "selected" : ""}>${serverOptionLabel(s)}</option>`)
        .join("");
    }
    function physicalServerOptions(value) {
      return state.servers
        .filter(s => s.id !== "auto" && s.enabled && s.user_visible && s.candidate_available !== false)
        .map(s => `<option value="${s.id}" ${s.id === value ? "selected" : ""}>${serverOptionLabel(s)}</option>`)
        .join("");
    }
    function physicalServers() {
      return state.servers.filter(s => s.id !== "auto" && s.enabled && s.user_visible && s.candidate_available !== false);
    }
    function userOptions(value) {
      return state.users.map(u => `<option value="${u.id}" ${u.id === value ? "selected" : ""}>${u.id}</option>`).join("");
    }
    function autoCandidateUserOptions(value) {
      return `<option value="" ${!value ? "selected" : ""}>Global</option>` + userOptions(value);
    }
    function fmtAge(seconds) {
      if (seconds === null || seconds === undefined) return "-";
      if (seconds < 90) return `${seconds}s`;
      const minutes = Math.round(seconds / 60);
      if (minutes < 90) return `${minutes}m`;
      const hours = Math.round(minutes / 60);
      if (hours < 72) return `${hours}h`;
      return `${Math.round(hours / 24)}d`;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }
    function badge(ok, text) {
      const cls = ok === true ? "ok" : ok === false ? "error" : "";
      return `<span class="badge ${cls}">${text}</span>`;
    }
    function renderSystemStatus() {
      const status = state.systemStatus;
      const text = document.getElementById("systemStatusText");
      const grid = document.getElementById("systemStatusGrid");
      const body = document.getElementById("systemStatusBody");
      if (!status) {
        text.textContent = "Status is not loaded.";
        text.className = "status";
        grid.innerHTML = "";
        body.innerHTML = '<tr><td colspan="4" class="muted">No status.</td></tr>';
        return;
      }
      const warnings = status.warnings || [];
      const advisories = status.advisories || [];
      text.textContent = status.ok ? "Control-server status is OK." : `Warnings: ${warnings.length}`;
      text.className = status.ok ? "status ok" : "status error";
      const autoWorker = (status.workers || {}).auto_probe || {};
      const providerWorker = (status.workers || {}).provider_refresh || {};
      const providerResult = providerWorker.last_result || {};
      const fallback = (status.control || {}).cudy_fallback_state || {};
      const backup = ((status.operations || {}).local_backup || {}).latest_archive || {};
      grid.innerHTML = [
        ["Service", badge(status.ok, status.ok ? "OK" : "WARN"), `uptime ${fmtAge((status.service || {}).uptime_seconds)}`],
        ["Agents", `${(status.agents || {}).online || 0}/${(status.agents || {}).enabled || 0}`, `recent ${(status.agents || {}).recent_seconds || "-"}s`],
        ["Probe jobs", `${(status.probe_jobs || {}).pending || 0} pending`, `${(status.probe_jobs || {}).failed_recent || 0}/${(status.probe_jobs || {}).failed || 0} recent failed`],
        ["Discovery", `${(status.domain_discovery || {}).pending || 0} pending`, `${(status.domain_discovery || {}).total || 0} total`],
        ["Transports", `${(status.transports || {}).active || 0}/${(status.transports || {}).enabled || 0}`, `active / enabled; stale ${(status.transports || {}).stale_enabled_count || 0}`],
        ["Auto worker", badge(autoWorker.enabled, autoWorker.enabled ? "on" : "off"), `last ${fmtAge(autoWorker.last_finished_age_seconds)}`],
        ["Provider worker", badge(providerWorker.enabled, providerWorker.enabled ? "on" : "off"), `${providerResult.refreshed ?? "-"} refreshed / ${providerResult.failed ?? "-"} failed`],
        ["Cudy fallback", fallback.reachable === false ? badge(null, "unreachable") : badge(fallback.ok, fallback.ok ? "fresh" : "stale"), `age ${fmtAge(fallback.age_seconds)}`],
        ["VPS backup cache", backup.exists ? "present" : "none", `operator pull is checked separately`],
      ].map(([label, value, detail]) => `
        <div class="summary-item">
          <div class="summary-label">${label}</div>
          <div class="summary-value">${value}</div>
          <div class="muted">${detail}</div>
        </div>
      `).join("");
      const providerDetails = Object.entries((status.transports || {}).providers || {})
        .map(([name, item]) => `${name}: ${item.active}/${item.enabled} active/enabled, refreshed ${fmtAge(item.newest_age_seconds)} ago`)
        .join("; ");
      const rows = [
        ["Workers", providerWorker.last_error || autoWorker.last_error ? "error" : "ok", `auto ${fmtAge(autoWorker.last_finished_age_seconds)} / provider ${fmtAge(providerWorker.last_finished_age_seconds)}`, `auto=${autoWorker.last_error || "-"}; provider=${providerWorker.last_error || "-"}; refresh=${providerResult.refreshed ?? "-"}/${providerResult.failed ?? "-"}`],
        ["Fallback", fallback.ok ? "ok" : "warn", fmtAge(fallback.age_seconds), fallback.error || fallback.archive_name || ""],
        ["Providers", "info", fmtAge((status.transports || {}).oldest_age_seconds), providerDetails || "-"],
        ["Probe jobs", (status.probe_jobs || {}).failed_recent ? "warn" : "ok", `updated ${fmtAge((status.probe_jobs || {}).latest_updated_age_seconds)}`, JSON.stringify((status.probe_jobs || {}).by_status || {})],
        ["Domain discovery", "info", `latest ${fmtAge((status.domain_discovery || {}).latest_seen_age_seconds)}`, JSON.stringify((status.domain_discovery || {}).by_status || {})],
        ["Operations", "info", `backup ${fmtAge(backup.age_seconds)}`, `backup=${backup.name || "-"}; fallback-log=${fmtAge((((status.operations || {}).local_cudy_fallback_sync || {}).task_log || {}).age_seconds)}`],
      ].concat(warnings.map(item => ["Warning", "warn", "-", item]))
        .concat(advisories.map(item => ["Advisory", "info", "-", item]));
      body.innerHTML = rows.map(([area, stateText, age, details]) => `
        <tr>
          <td>${area}</td>
          <td>${stateText === "ok" ? badge(true, "ok") : stateText === "warn" || stateText === "error" ? badge(false, stateText) : stateText}</td>
          <td>${age}</td>
          <td>${details}</td>
        </tr>
      `).join("");
    }
    function autoPolicyFor(userId, domain) {
      const normalizedUser = userId || "";
      const normalizedDomain = (domain || "").trim().toLowerCase();
      return state.autoCandidates.find(item =>
        (item.user_id || "") === normalizedUser &&
        (item.domain || "") === normalizedDomain &&
        item.enabled
      ) || null;
    }
    function autoPolicyLabel(userId, domain) {
      const policy = autoPolicyFor(userId, domain);
      if (!policy) return "";
      return (policy.candidate_server_ids || []).map(serverLabel).join(" -> ");
    }
    function setAutoEditor(prefix, serverIds) {
      autoEditors[prefix] = [...(serverIds || [])].map(value => value === "all-rest" ? ALL_REST : value);
      renderAutoEditor(prefix);
    }
    function parsePriorityText(text) {
      const aliases = new Map([
        ["all rest", ALL_REST],
        ["all-rest", ALL_REST],
        ["all_rest", ALL_REST],
        ["rest", ALL_REST],
      ]);
      return (text || "")
        .split(/[,\n;]+/)
        .map(item => item.trim())
        .filter(Boolean)
        .map(item => aliases.get(item.toLowerCase()) || item);
    }
    function formatPriorityText(values) {
      return (values || []).map(value => value === ALL_REST ? "all-rest" : value).join(", ");
    }
    function syncAutoText(prefix) {
      const input = document.getElementById(`${prefix}AutoText`);
      if (input && document.activeElement !== input) {
        input.value = formatPriorityText(autoEditors[prefix] || []);
      }
    }
    function renderRecentWinners(prefix, data) {
      const container = document.getElementById(`${prefix}AutoWinners`);
      if (!container) return;
      const winners = (data && data.winners) || [];
      const failures = (data && data.failures) || [];
      if (!winners.length && !failures.length) {
        container.textContent = "";
        return;
      }
      const winnerText = winners.map(item => {
        const latency = item.latency_ms == null ? "-" : `${item.latency_ms}ms`;
        const speed = item.speed_mbps == null ? "" : `, ${item.speed_mbps}Mbps`;
        const target = item.domain || "";
        return `${escapeHtml(item.winner_server_id || "-")} (${escapeHtml(latency + speed)}, ${escapeHtml(target)})`;
      }).join(" | ");
      const failureText = failures.slice(0, 3).map(item => {
        const checks = (item.checks || []).slice(0, 4).map(check =>
          `${escapeHtml(check.server_id || "-")}: ${escapeHtml(check.reason || "failed")}`
        ).join(", ");
        return `${escapeHtml(item.domain || "-")} (${checks || escapeHtml(item.reason || "failed")})`;
      }).join(" | ");
      container.innerHTML = [
        winnerText ? `Last winners: ${winnerText}` : "",
        failureText ? `Recent failures: ${failureText}` : ""
      ].filter(Boolean).join("<br>");
    }
    async function loadRecentWinners(prefix, target) {
      const container = document.getElementById(`${prefix}AutoWinners`);
      if (!container) return;
      const normalized = (target || "").trim();
      if (!normalized) {
        container.textContent = "";
        return;
      }
      try {
        const data = await api(`/api/admin/auto-winners?target=${encodeURIComponent(normalized)}&limit=10`);
        renderRecentWinners(prefix, data);
      } catch (error) {
        container.textContent = `Last winners unavailable: ${error.message}`;
      }
    }
    function selectedAutoCandidates(prefix) {
      return [...(autoEditors[prefix] || [])]
        .filter(Boolean)
        .map(value => value === ALL_REST ? "all-rest" : value);
    }
    async function savePriorityPolicy(userId, domain, prefix, statusId) {
      const status = document.getElementById(statusId);
      const candidates = selectedAutoCandidates(prefix);
      status.className = "status";
      if (candidates.length) {
        await api("/api/admin/auto-candidates", {
          method: "POST",
          body: JSON.stringify({
            user_id: userId,
            domain,
            candidate_server_ids: candidates
          })
        });
        status.textContent = "Priority saved.";
      } else {
        await api(`/api/admin/auto-candidates?user_id=${encodeURIComponent(userId)}&domain=${encodeURIComponent(domain)}`, { method: "DELETE" });
        status.textContent = "Priority cleared.";
      }
      status.className = "status ok";
    }
    function renderAutoEditor(prefix) {
      const field = document.getElementById(`${prefix}AutoField`);
      const container = document.getElementById(`${prefix}AutoList`);
      const serverSelect = prefix === "globalRoute"
        ? document.getElementById("globalRouteServer")
        : prefix === "adminRoute"
          ? document.getElementById("adminRouteServer")
          : null;
      const isAuto = !serverSelect || serverSelect.value === "auto";
      field.hidden = !isAuto;
      if (!isAuto) {
        container.innerHTML = "";
        syncAutoText(prefix);
        return;
      }
      const values = [...(autoEditors[prefix] || [])];
      if (!values.includes(ALL_REST)) values.push("");
      container.innerHTML = values.map((value, index) => {
        const selectedBefore = new Set(values.slice(0, index).filter(v => v && v !== ALL_REST));
        const options = ['<option value="">End</option>'];
        for (const server of physicalServers()) {
          if (!selectedBefore.has(server.id) || server.id === value) {
            options.push(`<option value="${server.id}" ${server.id === value ? "selected" : ""}>${server.label}</option>`);
          }
        }
        const remaining = physicalServers().some(server => !selectedBefore.has(server.id));
        if (remaining || value === ALL_REST) {
          options.push(`<option value="${ALL_REST}" ${value === ALL_REST ? "selected" : ""}>All rest</option>`);
        }
        return `<select data-auto-prefix="${prefix}" data-auto-index="${index}">${options.join("")}</select>`;
      }).join("");
      container.querySelectorAll("[data-auto-prefix]").forEach(select => {
        select.addEventListener("change", () => {
          const index = Number(select.dataset.autoIndex);
          const next = autoEditors[prefix].slice(0, index);
          if (select.value) next.push(select.value);
          autoEditors[prefix] = next;
          renderAutoEditor(prefix);
        });
      });
      syncAutoText(prefix);
    }
    function syncAutoEditorFromExisting(prefix) {
      if (prefix === "globalDefault") {
        const policy = autoPolicyFor("", "");
        setAutoEditor(prefix, policy ? policy.candidate_server_ids : []);
        return;
      }
      if (prefix === "userDefault") {
        const userId = document.getElementById("defaultRouteUser").value;
        const policy = autoPolicyFor(userId, "");
        setAutoEditor(prefix, policy ? policy.candidate_server_ids : []);
        return;
      }
      if (prefix === "globalRoute") {
        const domain = document.getElementById("globalRouteDomain").value;
        const policy = autoPolicyFor("", domain);
        setAutoEditor(prefix, policy ? policy.candidate_server_ids : []);
        loadRecentWinners(prefix, domain);
        return;
      }
      const userId = document.getElementById("routeUser").value;
      const domain = document.getElementById("adminRouteDomain").value;
      const policy = autoPolicyFor(userId, domain);
      setAutoEditor(prefix, policy ? policy.candidate_server_ids : []);
      loadRecentWinners(prefix, domain);
    }
    function renderServers() {
      const body = document.getElementById("serversBody");
      body.innerHTML = state.servers.map(s => `
        <tr data-id="${s.id}">
          <td>${s.id}</td>
          <td><input type="text" data-field="label" value="${s.label}"></td>
          <td>${s.provider}</td>
          <td>${s.interface || ""}</td>
          <td>${s.geo_region ? `${s.geo_country}-${s.geo_region}` : s.geo_country || ""}</td>
          <td>${s.transport_required ? (s.candidate_available ? "transport ok" : (s.transport_config_present ? `stale ${fmtAge(s.transport_age_seconds)}` : "missing transport")) : "ok"}</td>
          <td><input type="checkbox" data-field="enabled" ${s.enabled ? "checked" : ""}></td>
          <td><input type="checkbox" data-field="user_visible" ${s.user_visible ? "checked" : ""}></td>
          <td><button data-save="${s.id}">Save</button></td>
        </tr>
      `).join("");
      body.querySelectorAll("[data-save]").forEach(button => {
        button.addEventListener("click", async () => {
          const row = button.closest("tr");
          const payload = {
            id: button.dataset.save,
            label: row.querySelector('[data-field="label"]').value,
            enabled: row.querySelector('[data-field="enabled"]').checked,
            user_visible: row.querySelector('[data-field="user_visible"]').checked
          };
          const status = document.getElementById("serverStatus");
          status.className = "status";
          try {
            await api("/api/admin/servers", { method: "POST", body: JSON.stringify(payload) });
            status.textContent = "Saved.";
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
    }
    function renderUsers() {
      const body = document.getElementById("usersBody");
      const query = document.getElementById("userFilter").value.trim().toLowerCase();
      const users = state.users.filter(u => !query || [u.id, u.display_name, u.role, u.client_ip]
        .some(value => String(value || "").toLowerCase().includes(query)));
      document.getElementById("userFilterStatus").textContent = `Showing ${users.length} of ${state.users.length}`;
      body.innerHTML = users.map(u => `
        <tr data-id="${u.id}">
          <td>${u.id}</td>
          <td><input type="text" data-field="display_name" value="${u.display_name}"></td>
          <td><select data-field="role"><option value="user" ${u.role === "user" ? "selected" : ""}>user</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>admin</option></select></td>
          <td><input type="text" data-field="client_ip" value="${u.client_ip || ""}" placeholder="10.77.0.x"></td>
          <td><select data-field="default_server_id">${serverOptions(u.default_server_id)}</select></td>
          <td><input type="checkbox" data-field="enabled" ${u.enabled ? "checked" : ""}></td>
          <td title="Whether a web-panel password is configured">${u.has_login ? "configured" : "not set"}</td>
          <td class="inline">
            <input type="password" data-field="password" data-password-input="${u.id}" placeholder="new password only">
            <button class="secondary" type="button" data-toggle-row-password="${u.id}" title="Show/hide the new password typed here. Stored passwords cannot be viewed.">Show typed</button>
            <button class="secondary" data-password="${u.id}">Set</button>
          </td>
          <td class="inline">
            <button data-save-user="${u.id}">Save</button>
            <select data-delete-user-mode="${u.id}" title="Choose whether a legacy Cudy VPN peer should also be revoked.">
              <option value="local">Delete account only</option>
              <option value="revoke">Delete + revoke Cudy peer</option>
            </select>
            <button class="danger" data-delete-user="${u.id}">Delete user</button>
          </td>
        </tr>
      `).join("");
      body.querySelectorAll("[data-save-user]").forEach(button => {
        button.addEventListener("click", async () => {
          const row = button.closest("tr");
          const status = document.getElementById("userStatus");
          status.className = "status";
          try {
            await api("/api/admin/users", {
              method: "POST",
              body: JSON.stringify({
                id: button.dataset.saveUser,
                display_name: row.querySelector('[data-field="display_name"]').value,
                role: row.querySelector('[data-field="role"]').value,
                client_ip: row.querySelector('[data-field="client_ip"]').value,
                default_server_id: row.querySelector('[data-field="default_server_id"]').value,
                enabled: row.querySelector('[data-field="enabled"]').checked
              })
            });
            status.textContent = "Saved.";
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
      body.querySelectorAll("[data-password]").forEach(button => {
        button.addEventListener("click", async () => {
          const row = button.closest("tr");
          const password = row.querySelector('[data-field="password"]').value;
          const status = document.getElementById("userStatus");
          status.className = "status";
          try {
            await api("/api/admin/user-password", {
              method: "POST",
              body: JSON.stringify({ id: button.dataset.password, password })
            });
            row.querySelector('[data-field="password"]').value = "";
            status.textContent = "Password saved.";
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
      body.querySelectorAll("[data-toggle-row-password]").forEach(button => {
        button.addEventListener("click", () => {
          const input = button.closest("tr").querySelector('[data-field="password"]');
          const visible = input.type === "text";
          input.type = visible ? "password" : "text";
          button.textContent = visible ? "Show typed" : "Hide typed";
        });
      });
      body.querySelectorAll("[data-delete-user]").forEach(button => {
        button.addEventListener("click", async () => {
          const userId = button.dataset.deleteUser;
          const mode = button.closest("tr").querySelector("[data-delete-user-mode]").value;
          const revoke = mode === "revoke";
          const action = revoke ? "delete the account and revoke its Cudy VPN peer" : "delete the account only";
          if (!confirm(`Permanently ${action} for ${userId}?`)) return;
          const status = document.getElementById("userStatus");
          status.className = "status";
          try {
            const result = await api(`/api/admin/users?id=${encodeURIComponent(userId)}&revoke_cudy=${revoke ? "1" : "0"}`, { method: "DELETE" });
            status.textContent = `Deleted ${result.deleted_user_id}.`;
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
      document.getElementById("routeUser").innerHTML = userOptions(document.getElementById("routeUser").value);
      document.getElementById("lookupUser").innerHTML = userOptions(document.getElementById("lookupUser").value);
    }
    function renderLookupResult(result) {
      const body = document.getElementById("adminLookupBody");
      body.innerHTML = (result.results || []).map(item => `
        <tr>
          <td>${item.target}</td>
          <td>${item.route_state}</td>
          <td>${item.server_id === "direct" ? "direct" : serverLabel(item.server_id)}</td>
          <td>${item.matched_rule ? `${item.matched_rule.source}:${item.matched_rule.target_cidr || item.matched_rule.domain}` : "-"}</td>
          <td>${item.auto_cache ? `${item.auto_cache.selected_server_id || "-"} ${item.auto_cache.score_ms ?? ""}` : (item.auto_candidate_policy ? (item.auto_candidate_policy.candidate_server_ids || []).join(" -> ") : "-")}</td>
          <td>${(item.warnings || []).join("; ")}</td>
        </tr>
      `).join("") || '<tr><td colspan="6" class="muted">No result.</td></tr>';
    }
    function renderServiceAliases() {
      const body = document.getElementById("adminAliasesBody");
      body.innerHTML = state.serviceAliases.length ? state.serviceAliases.map(item => `
        <tr>
          <td>${item.alias}</td>
          <td>${item.label}</td>
          <td>${(item.targets || []).join(", ")}</td>
          <td class="muted">none; lookup shortcut only</td>
          <td><button class="danger" data-delete-alias="${item.alias}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="5" class="muted">No aliases.</td></tr>';
      body.querySelectorAll("[data-delete-alias]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/service-aliases?alias=${encodeURIComponent(button.dataset.deleteAlias)}`, { method: "DELETE" });
          await load();
        });
      });
    }
    function renderCriticalServices() {
      const body = document.getElementById("adminCriticalServicesBody");
      body.innerHTML = state.criticalServices.length ? state.criticalServices.map(item => `
        <tr>
          <td><strong>${escapeHtml(item.label)}</strong><br><span class="muted">${escapeHtml(item.service_key)}</span></td>
          <td>${item.user_id ? `User: ${escapeHtml(item.user_id)}` : "Global"}</td>
          <td>${(item.targets || []).map(escapeHtml).join("<br>")}</td>
          <td><span class="muted">success:</span> ${escapeHtml(item.success_pattern || "-")}<br><span class="muted">failure:</span> ${escapeHtml(item.failure_pattern || "-")}</td>
          <td>${item.routing_enabled ? `One Auto winner<br><span class="muted">${(item.candidate_server_ids || []).map(escapeHtml).join(" -> ")}</span>` : "health only"}</td>
          <td>${item.enabled ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-critical-key="${escapeHtml(item.service_key)}" data-delete-critical-user="${escapeHtml(item.user_id || "")}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="7" class="muted">No important services configured.</td></tr>';
      body.querySelectorAll("[data-delete-critical-key]").forEach(button => {
        button.addEventListener("click", async () => {
          const query = new URLSearchParams({ service_key: button.dataset.deleteCriticalKey, user_id: button.dataset.deleteCriticalUser });
          await api(`/api/admin/critical-services?${query}`, { method: "DELETE" });
          await load();
        });
      });
    }
    async function refreshDomainDiscovery(statusFilter) {
      const suffix = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : "";
      const data = await api(`/api/admin/domain-discovery${suffix}`);
      state.domainDiscovery = data.items || [];
      renderDomainDiscovery();
    }
    function renderDomainDiscovery() {
      const body = document.getElementById("domainDiscoveryBody");
      body.innerHTML = state.domainDiscovery.length ? state.domainDiscovery.map(item => `
        <tr>
          <td>${item.domain}</td>
          <td>${item.status}</td>
          <td>${item.hit_count}</td>
          <td>${(item.user_ids || []).join(", ")}</td>
          <td>${(item.client_ips || []).join(", ")}</td>
          <td>${item.last_seen_at || ""}</td>
          <td>${item.note || ""}</td>
          <td class="inline">
            <button class="secondary" data-promote-domain="${item.domain}">Use</button>
            <button data-promote-auto-domain="${item.domain}">Promote Auto</button>
            <button class="secondary" data-discovery-status="${item.domain}|reviewed">Reviewed</button>
            <button class="secondary" data-discovery-status="${item.domain}|pending">Pending</button>
            <button class="danger" data-discovery-status="${item.domain}|ignored">Ignore</button>
          </td>
        </tr>
      `).join("") : '<tr><td colspan="8" class="muted">No discovered domains.</td></tr>';
      body.querySelectorAll("[data-promote-domain]").forEach(button => {
        button.addEventListener("click", () => {
          const domain = button.dataset.promoteDomain;
          document.getElementById("globalRouteDomain").value = domain;
          document.getElementById("globalRouteServer").value = "auto";
          syncAutoEditorFromExisting("globalRoute");
          document.getElementById("globalRouteDomain").focus();
        });
      });
      body.querySelectorAll("[data-promote-auto-domain]").forEach(button => {
        button.addEventListener("click", async () => {
          const domain = button.dataset.promoteAutoDomain;
          const candidatesText = prompt("Candidate priority for Auto route. Leave blank to inherit default policy.", "");
          if (candidatesText === null) return;
          const note = prompt("Promotion note", "Promoted from discovery queue") || "";
          const probeNow = confirm("Create an Auto probe job now?");
          const statusEl = document.getElementById("domainDiscoveryStatus");
          statusEl.className = "status";
          try {
            const payload = {
              domain,
              user_id: "",
              candidate_server_ids: candidatesText.trim() ? parsePriorityText(candidatesText) : null,
              note,
              probe_now: probeNow
            };
            const result = await api("/api/admin/domain-discovery/promote", {
              method: "POST",
              body: JSON.stringify(payload)
            });
            const probe = result.probe_job && result.probe_job.created ? ` Probe job: ${result.probe_job.created.id}.` : "";
            statusEl.textContent = `${domain} promoted to ${result.route_scope} Auto route.${probe}`;
            statusEl.className = "status ok";
            await load();
          } catch (error) {
            statusEl.textContent = error.message;
            statusEl.className = "status error";
          }
        });
      });
      body.querySelectorAll("[data-discovery-status]").forEach(button => {
        button.addEventListener("click", async () => {
          const [domain, status] = button.dataset.discoveryStatus.split("|");
          const note = status === "ignored" ? (prompt("Note for ignored domain", "") || "") : "";
          const statusEl = document.getElementById("domainDiscoveryStatus");
          statusEl.className = "status";
          try {
            await api("/api/admin/domain-discovery", {
              method: "POST",
              body: JSON.stringify({ domain, status, note })
            });
            statusEl.textContent = `${domain} marked ${status}.`;
            statusEl.className = "status ok";
            await refreshDomainDiscovery(document.getElementById("domainDiscoveryStatusFilter").value);
          } catch (error) {
            statusEl.textContent = error.message;
            statusEl.className = "status error";
          }
        });
      });
    }
    function renderGlobalRoutes() {
      const body = document.getElementById("globalRoutesBody");
      body.innerHTML = state.globalRoutes.length ? state.globalRoutes.map(r => `
        <tr>
          <td>${r.domain}</td>
          <td>${r.server_id}</td>
          <td>${r.server_id === "auto" ? autoPolicyLabel("", r.domain) || '<span class="muted">Inherited/default</span>' : ""}</td>
          <td>${r.enabled ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-global-route="${r.domain}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="5" class="muted">No global routes.</td></tr>';
      body.querySelectorAll("[data-delete-global-route]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/admin/global-domain-routes?domain=${encodeURIComponent(button.dataset.deleteGlobalRoute)}`, { method: "DELETE" });
          await load();
        });
      });
    }
    function renderRoutes() {
      const body = document.getElementById("routesBody");
      body.innerHTML = state.routes.length ? state.routes.map(r => `
        <tr>
          <td>${r.user_id}</td>
          <td>${r.domain}</td>
          <td>${r.server_id}</td>
          <td>${r.server_id === "auto" ? autoPolicyLabel(r.user_id, r.domain) || '<span class="muted">Inherited/default</span>' : ""}</td>
          <td>${r.enabled ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-route="${r.user_id}|${r.domain}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="6" class="muted">No routes.</td></tr>';
      body.querySelectorAll("[data-delete-route]").forEach(button => {
        button.addEventListener("click", async () => {
          const [userId, domain] = button.dataset.deleteRoute.split("|");
          await api(`/api/admin/domain-routes?user_id=${encodeURIComponent(userId)}&domain=${encodeURIComponent(domain)}`, { method: "DELETE" });
          await load();
        });
      });
    }
    function renderAutoCache() {
      const body = document.getElementById("autoCacheBody");
      body.innerHTML = state.autoCache.length ? state.autoCache.map(r => `
        <tr>
          <td>${r.domain}</td>
          <td>${r.selected_server_id || ""}</td>
          <td>${r.score_ms ?? ""}</td>
          <td>${r.status}</td>
          <td>${r.checked_at || ""}</td>
          <td><button class="danger" data-delete-auto-cache="${r.domain}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="6" class="muted">No cached Auto choices.</td></tr>';
      body.querySelectorAll("[data-delete-auto-cache]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/admin/auto-cache?domain=${encodeURIComponent(button.dataset.deleteAutoCache)}`, { method: "DELETE" });
          await load();
        });
      });
    }
    function renderAutoProbeJobs() {
      const body = document.getElementById("autoProbeJobsBody");
      body.innerHTML = state.probeJobs.length ? state.probeJobs.map(job => `
        <tr>
          <td>${job.status}</td>
          <td>${job.domain}</td>
          <td>${(job.candidate_server_ids || []).join(" -> ")}</td>
          <td>${job.assigned_device_id || ""}</td>
          <td>${job.claimed_by_device_id || ""}</td>
          <td>${job.winner_server_id || ""}</td>
          <td>${job.score_ms ?? ""}</td>
          <td>${job.updated_at || ""}</td>
        </tr>
      `).join("") : '<tr><td colspan="8" class="muted">No probe jobs.</td></tr>';
    }
    function renderAgentStatus() {
      const body = document.getElementById("agentStatusBody");
      const query = document.getElementById("agentFilter").value.trim().toLowerCase();
      const agents = state.agentStatus.filter(item => !query || [item.device_id, item.user_id, item.platform]
        .some(value => String(value || "").toLowerCase().includes(query)));
      document.getElementById("agentFilterStatus").textContent = `Showing ${agents.length} of ${state.agentStatus.length}`;
      body.innerHTML = agents.length ? agents.map(item => {
        const health = (item.status || {}).health || {};
        const errors = ((item.status || {}).errors || []).concat((item.status || {}).status_errors || []);
        return `
          <tr>
            <td>${item.device_id}</td>
            <td>${item.user_id}</td>
            <td>${item.platform || ""}</td>
            <td><input type="checkbox" data-agent-enabled="${item.device_id}" ${item.enabled ? "checked" : ""}></td>
            <td>${item.last_seen_at || ""}</td>
            <td>${item.reported_at || ""}</td>
            <td>${health.ok === true ? "ok" : health.ok === false ? "fail" : ""}</td>
            <td>${health.applied ?? ""}</td>
            <td>${errors.length ? errors.slice(0, 2).join("; ") : ""}</td>
            <td class="inline">
              <button data-save-agent="${item.device_id}">Apply state</button>
              <button class="danger" data-delete-agent="${item.device_id}">Delete device</button>
            </td>
          </tr>
        `;
      }).join("") : '<tr><td colspan="10" class="muted">No agent status.</td></tr>';
      body.querySelectorAll("[data-save-agent]").forEach(button => {
        button.addEventListener("click", async () => {
          const deviceId = button.dataset.saveAgent;
          const enabled = button.closest("tr").querySelector("[data-agent-enabled]").checked;
          const previous = state.agentStatus.find(item => item.device_id === deviceId);
          if (previous && Boolean(previous.enabled) !== enabled) {
            const action = enabled ? "enable" : "disable";
            if (!confirm(`${action[0].toUpperCase() + action.slice(1)} agent device ${deviceId}?`)) return;
          }
          const status = document.getElementById("enrollmentStatus");
          status.className = "status";
          try {
            await api("/api/admin/agent-devices", {
              method: "POST",
              body: JSON.stringify({ id: deviceId, enabled })
            });
            status.textContent = `Agent device ${enabled ? "enabled" : "disabled"}: ${deviceId}`;
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
      body.querySelectorAll("[data-delete-agent]").forEach(button => {
        button.addEventListener("click", async () => {
          const deviceId = button.dataset.deleteAgent;
          if (!confirm(`Delete agent device ${deviceId} permanently?`)) return;
          const status = document.getElementById("enrollmentStatus");
          status.className = "status";
          try {
            await api(`/api/admin/agent-devices?id=${encodeURIComponent(deviceId)}&hard=1`, { method: "DELETE" });
            status.textContent = `Agent device deleted: ${deviceId}`;
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
    }
    function renderAgentDiagnostics() {
      const body = document.getElementById("agentDiagnosticsBody");
      const report = document.getElementById("agentDiagnosticReport");
      body.innerHTML = state.agentDiagnostics.length ? state.agentDiagnostics.map(item => `
        <tr>
          <td>${item.created_at || ""}</td>
          <td>${item.device_id || ""}</td>
          <td>${item.user_id || ""}</td>
          <td>${item.platform || ""}</td>
          <td>${escapeHtml(item.summary || "")}</td>
          <td><button data-view-diagnostic="${item.id}">View</button></td>
        </tr>
      `).join("") : '<tr><td colspan="6" class="muted">No diagnostics.</td></tr>';
      body.querySelectorAll("[data-view-diagnostic]").forEach(button => {
        button.addEventListener("click", () => {
          const item = state.agentDiagnostics.find(row => row.id === button.dataset.viewDiagnostic);
          report.textContent = item ? item.report_text || "" : "";
        });
      });
    }
    function renderEnrollmentCodes() {
      const body = document.getElementById("enrollmentCodesBody");
      body.innerHTML = state.enrollmentCodes.length ? state.enrollmentCodes.map(item => {
        const stateText = item.used_at ? "used" : (item.enabled ? "active" : "disabled");
        const device = item.used_device_id || item.desired_device_id || "";
        return `
          <tr>
            <td>${item.id}</td>
            <td>${item.user_id}</td>
            <td>${device}</td>
            <td>${item.platform || ""}</td>
            <td>${stateText}</td>
            <td>${item.expires_at || ""}</td>
            <td>${item.updated_at || ""}</td>
            <td>${!item.used_at && item.enabled ? `<button class="danger" data-revoke-enrollment="${item.id}">Revoke</button>` : ""}</td>
          </tr>
        `;
      }).join("") : '<tr><td colspan="8" class="muted">No enrollment codes.</td></tr>';
      body.querySelectorAll("[data-revoke-enrollment]").forEach(button => {
        button.addEventListener("click", async () => {
          const status = document.getElementById("enrollmentStatus");
          if (!confirm(`Revoke one-time enrollment code ${button.dataset.revokeEnrollment}?`)) return;
          status.className = "status";
          try {
            await api(`/api/admin/enrollment-codes?id=${encodeURIComponent(button.dataset.revokeEnrollment)}`, { method: "DELETE" });
            status.textContent = "Enrollment code revoked.";
            status.className = "status ok";
            await load();
          } catch (error) {
            status.textContent = error.message;
            status.className = "status error";
          }
        });
      });
    }
    function renderAgentUpdates() {
      const body = document.getElementById("agentUpdatesBody");
      body.innerHTML = state.agentUpdates.length ? state.agentUpdates.map(item => {
        const packageText = item.download_url
          ? `<span class="badge ok">available</span> ${item.download_url}`
          : '<span class="badge">not built</span>';
        return `
          <tr>
            <td>${item.platform}</td>
            <td>${item.version_name || ""}</td>
            <td>${item.version_code ?? ""}</td>
            <td>${packageText}</td>
            <td>${item.sha256 ? item.sha256.slice(0, 12) + "..." : ""}</td>
            <td>${item.release_notes || ""}</td>
          </tr>
        `;
      }).join("") : '<tr><td colspan="6" class="muted">No agent update manifests.</td></tr>';
    }
    function renderProviderTransports() {
      const body = document.getElementById("providerTransportsBody");
      const rows = state.transportConfigs || [];
      body.innerHTML = rows.length ? rows.map(item => `
        <tr>
          <td>${item.server_id}</td>
          <td>${item.provider || ""}</td>
          <td>${item.transport_type}</td>
          <td>${item.interface_name}</td>
          <td>${item.endpoint || ""}</td>
          <td>${item.source || ""}</td>
          <td>${item.version || ""}</td>
          <td>${item.updated_at || ""}</td>
          <td>${item.enabled ? "yes" : "no"}</td>
        </tr>
      `).join("") : '<tr><td colspan="9" class="muted">No provider transports.</td></tr>';
    }
    document.getElementById("userFilter").addEventListener("input", renderUsers);
    document.getElementById("agentFilter").addEventListener("input", renderAgentStatus);
    async function load() {
      const [data, statusResult] = await Promise.all([
        api("/api/admin"),
        api("/api/status").catch(error => ({ ok: false, warnings: [error.message] }))
      ]);
      state.systemStatus = statusResult;
      state.servers = data.servers;
      state.users = data.users;
      state.routes = data.routes;
      state.globalRoutes = data.global_routes || [];
      state.autoCache = data.auto_cache || [];
      state.autoCandidates = data.auto_candidates || [];
      state.probeJobs = data.probe_jobs || [];
      state.agentStatus = data.agent_status || [];
      state.agentDiagnostics = data.agent_diagnostics || [];
      state.enrollmentCodes = data.agent_enrollment_codes || [];
      state.agentUpdates = data.agent_updates || [];
      state.transportConfigs = data.transport_configs || [];
      state.serviceAliases = data.service_aliases || [];
      state.criticalServices = data.critical_services || [];
      state.domainDiscovery = data.domain_discovery || [];
      renderServers();
      renderUsers();
      renderServiceAliases();
      renderCriticalServices();
      renderDomainDiscovery();
      renderSystemStatus();
      renderGlobalRoutes();
      renderRoutes();
      renderAutoCache();
      renderAutoProbeJobs();
      renderAgentStatus();
      renderAgentDiagnostics();
      renderEnrollmentCodes();
      renderAgentUpdates();
      renderProviderTransports();
      document.getElementById("adminRouteServer").innerHTML = serverOptions(document.getElementById("adminRouteServer").value || "auto");
      document.getElementById("globalRouteServer").innerHTML = serverOptions(document.getElementById("globalRouteServer").value || "auto");
      document.getElementById("autoCacheServer").innerHTML = physicalServerOptions(document.getElementById("autoCacheServer").value);
      document.getElementById("autoSelectUser").innerHTML = autoCandidateUserOptions(document.getElementById("autoSelectUser").value);
      document.getElementById("enrollmentUser").innerHTML = userOptions(document.getElementById("enrollmentUser").value);
      document.getElementById("adminCriticalServiceUser").innerHTML = '<option value="">Global</option>' + state.users.map(user => `<option value="${escapeHtml(user.id)}">${escapeHtml(user.display_name)} (${escapeHtml(user.id)})</option>`).join("");
      document.getElementById("routeUser").innerHTML = userOptions(document.getElementById("routeUser").value);
      document.getElementById("defaultRouteUser").innerHTML = userOptions(document.getElementById("defaultRouteUser").value);
      syncAutoEditorFromExisting("globalDefault");
      syncAutoEditorFromExisting("userDefault");
      renderAutoEditor("globalRoute");
      renderAutoEditor("adminRoute");
    }
    document.getElementById("newUserForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("userStatus");
      status.className = "status";
      try {
        const result = await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({
            id: document.getElementById("newUserId").value,
            display_name: document.getElementById("newUserName").value || document.getElementById("newUserId").value,
            role: document.getElementById("newUserRole").value,
            client_ip: document.getElementById("newUserClientIp").value,
            default_server_id: "auto",
            enabled: true,
            create_cudy_client: document.getElementById("newUserCreateCudy").checked,
            password: document.getElementById("newUserPassword").value
          })
        });
        event.target.reset();
        status.textContent = result.cudy_client
          ? `User created. Legacy Cudy peer created: ${result.cudy_client.config_path || result.cudy_client.client_name || result.id || ""}`
          : "User created. Create a one-time agent enrollment code below.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("syncCudyClients").addEventListener("click", async () => {
      const status = document.getElementById("userStatus");
      status.className = "status";
      status.textContent = "Syncing Cudy clients...";
      try {
        const result = await api("/api/admin/sync-cudy-clients", { method: "POST", body: "{}" });
        status.textContent = `Synced Cudy clients: ${result.synced.length}, warnings: ${result.warnings.length}`;
        status.className = result.warnings.length ? "status error" : "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("enrollmentForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("enrollmentStatus");
      status.className = "status";
      try {
        const result = await api("/api/admin/enrollment-codes", {
          method: "POST",
          body: JSON.stringify({
            user_id: document.getElementById("enrollmentUser").value,
            device_id: document.getElementById("enrollmentDeviceId").value,
            display_name: document.getElementById("enrollmentDisplayName").value,
            platform: document.getElementById("enrollmentPlatform").value,
            ttl_hours: Number(document.getElementById("enrollmentTtlHours").value || 24)
          })
        });
        document.getElementById("enrollmentDeviceId").value = "";
        document.getElementById("enrollmentDisplayName").value = "";
        status.textContent = `Activation code: ${result.code} (expires ${result.expires_at})`;
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("refreshSystemStatus").addEventListener("click", async () => {
      const text = document.getElementById("systemStatusText");
      text.textContent = "Refreshing status...";
      text.className = "status";
      try {
        state.systemStatus = await api("/api/status");
      } catch (error) {
        state.systemStatus = { ok: false, warnings: [error.message] };
      }
      renderSystemStatus();
    });
    document.getElementById("refreshDomainDiscovery").addEventListener("click", async () => {
      const statusEl = document.getElementById("domainDiscoveryStatus");
      statusEl.className = "status";
      try {
        await refreshDomainDiscovery(document.getElementById("domainDiscoveryStatusFilter").value);
        statusEl.textContent = "Refreshed.";
        statusEl.className = "status ok";
      } catch (error) {
        statusEl.textContent = error.message;
        statusEl.className = "status error";
      }
    });
    document.getElementById("domainDiscoveryStatusFilter").addEventListener("change", async event => {
      await refreshDomainDiscovery(event.target.value);
    });
    document.getElementById("adminLookupForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("adminLookupStatus");
      status.className = "status";
      try {
        const result = await api(`/api/route-lookup?user_id=${encodeURIComponent(document.getElementById("lookupUser").value)}&target=${encodeURIComponent(document.getElementById("adminLookupTarget").value)}`);
        renderLookupResult(result);
        status.textContent = result.alias ? `Alias ${result.alias.label}: ${result.alias.targets.length} target(s).` : "Lookup complete.";
        status.className = "status ok";
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("adminAliasForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("adminAliasStatus");
      status.className = "status";
      try {
        await api("/api/service-aliases", {
          method: "POST",
          body: JSON.stringify({
            alias: document.getElementById("adminAliasInput").value,
            label: document.getElementById("adminAliasLabel").value,
            targets: document.getElementById("adminAliasTargets").value
          })
        });
        event.target.reset();
        status.textContent = "Alias saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("adminCriticalServiceForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("adminCriticalServiceStatus");
      status.className = "status";
      try {
        await api("/api/admin/critical-services", {
          method: "POST",
          body: JSON.stringify({
            user_id: document.getElementById("adminCriticalServiceUser").value,
            service_key: document.getElementById("adminCriticalServiceKey").value,
            label: document.getElementById("adminCriticalServiceLabel").value,
            targets: document.getElementById("adminCriticalServiceTargets").value,
            success_pattern: document.getElementById("adminCriticalServiceSuccess").value,
            failure_pattern: document.getElementById("adminCriticalServiceFailure").value,
            routing_enabled: document.getElementById("adminCriticalServiceRouting").checked,
            candidate_server_ids: document.getElementById("adminCriticalServiceCandidates").value,
            enabled: document.getElementById("adminCriticalServiceEnabled").checked
          })
        });
        event.target.reset();
        document.getElementById("adminCriticalServiceRouting").checked = false;
        document.getElementById("adminCriticalServiceEnabled").checked = true;
        status.textContent = "Saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.querySelectorAll("[data-toggle-password]").forEach(button => {
      button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.togglePassword);
        const visible = input.type === "text";
        input.type = visible ? "password" : "text";
        button.textContent = visible ? "Show typed" : "Hide typed";
      });
    });
    document.getElementById("globalRouteForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("globalRouteStatus");
      status.className = "status";
      try {
        await api("/api/admin/global-domain-routes", {
          method: "POST",
          body: JSON.stringify({
            domain: document.getElementById("globalRouteDomain").value,
            server_id: document.getElementById("globalRouteServer").value,
            auto_candidate_server_ids: selectedAutoCandidates("globalRoute")
          })
        });
        document.getElementById("globalRouteDomain").value = "";
        setAutoEditor("globalRoute", []);
        status.textContent = "Global route saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("globalDefaultPriorityForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("globalDefaultPriorityStatus");
      try {
        await savePriorityPolicy("", "", "globalDefault", "globalDefaultPriorityStatus");
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("adminRouteForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("adminRouteStatus");
      status.className = "status";
      try {
        await api("/api/admin/domain-routes", {
          method: "POST",
          body: JSON.stringify({
            user_id: document.getElementById("routeUser").value,
            domain: document.getElementById("adminRouteDomain").value,
            server_id: document.getElementById("adminRouteServer").value,
            auto_candidate_server_ids: selectedAutoCandidates("adminRoute")
          })
        });
        document.getElementById("adminRouteDomain").value = "";
        setAutoEditor("adminRoute", []);
        status.textContent = "Route saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("userDefaultPriorityForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("userDefaultPriorityStatus");
      try {
        await savePriorityPolicy(document.getElementById("defaultRouteUser").value, "", "userDefault", "userDefaultPriorityStatus");
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("autoCacheForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("autoCacheStatus");
      status.className = "status";
      try {
        const scoreRaw = document.getElementById("autoCacheScore").value;
        await api("/api/admin/auto-cache", {
          method: "POST",
          body: JSON.stringify({
            domain: document.getElementById("autoCacheDomain").value,
            selected_server_id: document.getElementById("autoCacheServer").value,
            score_ms: scoreRaw === "" ? null : Number(scoreRaw),
            status: document.getElementById("autoCacheStatusInput").value || "manual"
          })
        });
        document.getElementById("autoCacheDomain").value = "";
        document.getElementById("autoCacheScore").value = "";
        status.textContent = "Auto cache saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("autoSelectForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("autoSelectStatus");
      status.className = "status";
      status.textContent = "Running Auto probe...";
      try {
        const result = await api("/api/admin/auto-select", {
          method: "POST",
          body: JSON.stringify({
            domain: document.getElementById("autoSelectDomain").value,
            user_id: document.getElementById("autoSelectUser").value,
            candidate_server_ids: document.getElementById("autoSelectCandidates").value,
            apply: document.getElementById("autoSelectApply").checked,
            deploy: document.getElementById("autoSelectDeploy").checked,
            switch_profiles: document.getElementById("autoSelectProfiles").checked
          })
        });
        const winner = result.winner;
        const summary = result.checks.map(c => `${c.server_id}:${c.status}${c.score_ms ? `/${c.score_ms}ms` : ""}`).join(", ");
        status.textContent = winner
          ? `Winner ${winner.server_id} (${winner.score_ms || "-"} ms). ${summary}`
          : `No working candidate. ${summary}`;
        status.className = winner ? "status ok" : "status error";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("runAutoWorker").addEventListener("click", async () => {
      const status = document.getElementById("autoProbeStatus");
      status.className = "status";
      status.textContent = "Creating due probe jobs...";
      try {
        const result = await api("/api/admin/auto-worker-once", {
          method: "POST",
          body: JSON.stringify({
            max_jobs: Number(document.getElementById("autoWorkerMaxJobs").value || 5),
            max_candidates_per_job: Number(document.getElementById("autoWorkerMaxCandidates").value || 4),
            cache_ttl_seconds: Number(document.getElementById("autoWorkerCacheTtl").value || 3600)
          })
        });
        status.textContent = `Created ${result.created.length}, skipped ${result.skipped.length}, active agents ${result.active_agents}.`;
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("refreshAutoJobs").addEventListener("click", load);
    document.getElementById("refreshProviderTransports").addEventListener("click", load);
    document.getElementById("runProviderRefresh").addEventListener("click", async () => {
      const status = document.getElementById("providerRefreshStatus");
      status.className = "status";
      status.textContent = "Refreshing provider transports...";
      try {
        const result = await api("/api/admin/provider-refresh", {
          method: "POST",
          body: JSON.stringify({
            provider: document.getElementById("providerRefreshProvider").value,
            servers: document.getElementById("providerRefreshServers").value,
            skip_verify: document.getElementById("providerRefreshSkipVerify").checked
          })
        });
        const groups = result.provider === "all" ? (result.results || []) : [result];
        const refreshed = groups.reduce((sum, group) => sum + (group.refreshed || []).length, 0);
        const failed = groups.reduce((sum, group) => sum + (group.failed || []).length, 0);
        const failedText = groups.flatMap(group => group.failed || []).slice(0, 3).map(item => `${item.server_id}: ${item.error}`).join("; ");
        status.textContent = `Refreshed ${refreshed}, failed ${failed}.${failedText ? " " + failedText : ""}`;
        status.className = failed ? "status error" : "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("globalRouteServer").addEventListener("change", () => renderAutoEditor("globalRoute"));
    document.getElementById("adminRouteServer").addEventListener("change", () => renderAutoEditor("adminRoute"));
    let autoHistoryTimer = null;
    function scheduleAutoHistory(prefix) {
      if (autoHistoryTimer) clearTimeout(autoHistoryTimer);
      autoHistoryTimer = setTimeout(() => syncAutoEditorFromExisting(prefix), 250);
    }
    document.getElementById("globalRouteDomain").addEventListener("input", () => scheduleAutoHistory("globalRoute"));
    document.getElementById("adminRouteDomain").addEventListener("input", () => scheduleAutoHistory("adminRoute"));
    document.getElementById("globalRouteDomain").addEventListener("change", () => syncAutoEditorFromExisting("globalRoute"));
    document.getElementById("adminRouteDomain").addEventListener("change", () => syncAutoEditorFromExisting("adminRoute"));
    document.getElementById("routeUser").addEventListener("change", () => syncAutoEditorFromExisting("adminRoute"));
    document.getElementById("defaultRouteUser").addEventListener("change", () => syncAutoEditorFromExisting("userDefault"));
    for (const prefix of ["globalDefault", "userDefault", "globalRoute", "adminRoute"]) {
      document.getElementById(`${prefix}AutoText`).addEventListener("input", event => {
        autoEditors[prefix] = parsePriorityText(event.target.value);
        renderAutoEditor(prefix);
      });
    }
    document.getElementById("refreshPlan").addEventListener("click", async () => {
      const status = document.getElementById("planStatus");
      status.className = "status";
      try {
        const plan = await api("/api/route-plan");
        document.getElementById("routePlan").textContent = JSON.stringify(plan, null, 2);
        status.textContent = `Users: ${plan.summary.users}, effective routes: ${plan.summary.effective_routes}, warnings: ${plan.summary.warnings}`;
        status.className = plan.summary.warnings ? "status error" : "status ok";
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("refreshDeployPlan").addEventListener("click", async () => {
      const status = document.getElementById("planStatus");
      status.className = "status";
      try {
        const plan = await api("/api/admin/deploy-preview");
        document.getElementById("deployPlan").textContent = JSON.stringify(plan, null, 2);
        status.textContent = `Global routes: ${plan.summary.global_routes} apply=${plan.summary.apply_global}; user routes: ${plan.summary.user_routes} apply=${plan.summary.apply_user}; warnings: ${plan.summary.warnings}`;
        status.className = plan.summary.warnings ? "status error" : "status ok";
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("applyDeployPlan").addEventListener("click", async () => {
      const status = document.getElementById("planStatus");
      const installScripts = document.getElementById("applyInstallScripts").checked;
      const message = installScripts
        ? "Apply routes and reinstall OpenWrt scripts now?"
        : "Apply routes to Cudy now?";
      if (!confirm(message)) return;
      status.className = "status";
      status.textContent = "Applying routes...";
      try {
        const result = await api("/api/admin/deploy-routes", {
          method: "POST",
          body: JSON.stringify({ install_scripts: installScripts })
        });
        document.getElementById("deployPlan").textContent = JSON.stringify(result, null, 2);
        status.textContent = `Applied. PBR=${result.results.pbr ? "yes" : "skipped"}, user routes=${result.results.user_routes ? "yes" : "skipped"}`;
        status.className = "status ok";
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
    document.getElementById("logoutButton").addEventListener("click", async () => {
      await api("/api/logout", { method: "POST", body: "{}" });
      location.href = "/login";
    });
    load().catch(error => {
      document.getElementById("serverStatus").textContent = error.message;
      document.getElementById("serverStatus").className = "status error";
    });
  </script>
</body>
</html>
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def split_csv_env(value: str) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[\s,;]+", value or ""):
        item = item.strip()
        if item:
            result.append(item)
    return result


def control_endpoints_manifest(*, valid_for_seconds: int = 600, cache_seconds: int = 300) -> dict[str, Any]:
    generated_at = now()
    valid_until = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=valid_for_seconds)
    ).isoformat()
    primary_url = os.environ.get("VPN_CONTROL_PRIMARY_URL", DEFAULT_CONTROL_PRIMARY_URL).strip()
    primary_host = os.environ.get("VPN_CONTROL_PRIMARY_SSH_HOST", DEFAULT_CONTROL_PRIMARY_SSH_HOST).strip()
    primary_user = os.environ.get("VPN_CONTROL_PRIMARY_SSH_USER", DEFAULT_CONTROL_PRIMARY_SSH_USER).strip()
    primary_remote_port = int(os.environ.get("VPN_CONTROL_PRIMARY_REMOTE_PORT", "8765"))
    fallback_urls = split_csv_env(os.environ.get("VPN_CONTROL_FALLBACK_URLS", DEFAULT_CONTROL_FALLBACK_URLS))
    endpoints: list[dict[str, Any]] = []
    if primary_url:
        primary: dict[str, Any] = {
            "id": "primary",
            "role": "primary",
            "url": primary_url,
            "priority": 10,
        }
        if primary_host:
            primary["ssh_tunnel"] = {
                "host": primary_host,
                "user": primary_user,
                "remote_host": "127.0.0.1",
                "remote_port": primary_remote_port,
            }
        endpoints.append(primary)
    for index, url in enumerate(fallback_urls, start=1):
        endpoints.append(
            {
                "id": f"fallback-{index}",
                "role": "fallback",
                "url": url,
                "priority": 100 + index,
            }
        )
    return {
        "schema_version": 1,
        "generation": os.environ.get("VPN_CONTROL_GENERATION", "manual"),
        "generated_at": generated_at,
        "valid_until": valid_until,
        "cache_seconds": cache_seconds,
        "endpoints": endpoints,
    }


def agent_app_version_manifest(platform: str) -> dict[str, Any]:
    normalized_platform = normalize_platform(platform) or "android"
    version_path = AGENT_UPDATE_DIR / f"{normalized_platform}.version.json"
    artifact_suffix = ".apk" if normalized_platform == "android" else ".zip"
    artifact_path = AGENT_UPDATE_DIR / f"{normalized_platform}{artifact_suffix}"
    file_version: dict[str, Any] = {}
    if version_path.exists():
        try:
            file_version = json.loads(version_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            file_version = {}
    if normalized_platform == "android":
        version_name = (os.environ.get("CUDY_ANDROID_VERSION_NAME") or str(file_version.get("version_name") or "1.0")).strip() or "1.0"
        version_code = int(os.environ.get("CUDY_ANDROID_VERSION_CODE") or str(file_version.get("version_code") or "1") or "1")
        download_url = os.environ.get("CUDY_ANDROID_APK_URL", "").strip()
        sha256 = (os.environ.get("CUDY_ANDROID_APK_SHA256") or str(file_version.get("sha256") or "")).strip()
        release_notes = os.environ.get("CUDY_ANDROID_RELEASE_NOTES", "").strip()
    else:
        env_prefix = f"CUDY_{normalized_platform.upper()}"
        version_name = (os.environ.get(f"{env_prefix}_VERSION_NAME") or str(file_version.get("version_name") or "")).strip()
        version_code = int(os.environ.get(f"{env_prefix}_VERSION_CODE") or str(file_version.get("version_code") or "0") or "0")
        download_url = os.environ.get(f"{env_prefix}_DOWNLOAD_URL", "").strip()
        sha256 = (os.environ.get(f"{env_prefix}_SHA256") or str(file_version.get("sha256") or "")).strip()
        release_notes = os.environ.get(f"{env_prefix}_RELEASE_NOTES", "").strip()
    if not download_url and artifact_path.exists():
        download_url = f"/api/agent/update-package?platform={normalized_platform}"
    if artifact_path.exists() and not sha256:
        sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return {
        "ok": True,
        "platform": normalized_platform,
        "version_name": version_name,
        "version_code": version_code,
        "download_url": download_url,
        "sha256": sha256,
        "release_notes": release_notes,
        "generated_at": now(),
    }


def fetch_json_url(url: str, *, timeout: int = 3) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "cudy-control-status/1"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object from {url}")
    return payload


def cudy_fallback_state_status() -> dict[str, Any]:
    checked_at = time.monotonic()
    with FALLBACK_STATUS_CACHE_LOCK:
        cached = FALLBACK_STATUS_CACHE.get("value")
        cached_at = float(FALLBACK_STATUS_CACHE.get("checked_at") or 0.0)
        if cached is not None and checked_at - cached_at <= FALLBACK_STATUS_CACHE_SECONDS:
            return dict(cached)
    result: dict[str, Any] = {
        "url": CUDY_FALLBACK_STATE_URL,
        "reachable": False,
        "ok": False,
    }
    if not CUDY_FALLBACK_STATE_URL:
        result["error"] = "CUDY_FALLBACK_STATE_URL is empty"
        return result
    try:
        payload = fetch_json_url(CUDY_FALLBACK_STATE_URL, timeout=3)
        age = timestamp_age_seconds(str(payload.get("created_at") or ""))
        result.update(
            {
                "reachable": True,
                "created_at": payload.get("created_at"),
                "age_seconds": age,
                "bytes": payload.get("bytes"),
                "archive_name": payload.get("archive_name"),
                "source_host": payload.get("source_host"),
                "sha256": payload.get("sha256"),
                "ok": age is not None and age <= CUDY_FALLBACK_MAX_AGE_SECONDS,
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
    with FALLBACK_STATUS_CACHE_LOCK:
        FALLBACK_STATUS_CACHE["checked_at"] = checked_at
        FALLBACK_STATUS_CACHE["value"] = dict(result)
    return result


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_age_seconds(value: str | None, *, reference: datetime | None = None) -> int | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    reference = reference or datetime.now(timezone.utc)
    return max(0, int((reference - parsed).total_seconds()))


def file_status(path: Path, *, reference: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
    }
    try:
        stat = path.stat()
    except OSError as exc:
        result["error"] = str(exc)
        return result
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0)
    result.update(
        {
            "exists": True,
            "bytes": stat.st_size,
            "modified_at": modified.isoformat(),
            "age_seconds": timestamp_age_seconds(modified.isoformat(), reference=reference),
        }
    )
    return result


def latest_file_status(directory: Path, pattern: str, *, reference: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "directory": str(directory),
        "pattern": pattern,
        "exists": False,
    }
    try:
        candidates = [item for item in directory.glob(pattern) if item.is_file()]
    except OSError as exc:
        result["error"] = str(exc)
        return result
    if not candidates:
        return result
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    status = file_status(latest, reference=reference)
    status.update(
        {
            "directory": str(directory),
            "pattern": pattern,
            "name": latest.name,
            "count": len(candidates),
        }
    )
    return status


def persist_worker_status(db_path: Path, name: str, status: dict[str, Any]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO worker_status (name, status_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              status_json = excluded.status_json,
              updated_at = excluded.updated_at
            """,
            (name, json.dumps(status, ensure_ascii=False, sort_keys=True), now()),
        )


def update_worker_status(name: str, db_path: Path | None = None, **fields: Any) -> None:
    timestamp = now()
    with WORKER_STATUS_LOCK:
        current = WORKER_STATUS.setdefault(name, {})
        current.update(fields)
        if fields.get("started"):
            current["last_started_at"] = timestamp
        if fields.get("finished"):
            current["last_finished_at"] = timestamp
        current.pop("started", None)
        current.pop("finished", None)
        snapshot = json.loads(json.dumps(current))
    if db_path is not None:
        try:
            persist_worker_status(db_path, name, snapshot)
        except Exception as exc:
            print(f"worker status persist failed for {name}: {exc}", file=sys.stderr)


def worker_status_snapshot(db_path: Path | None = None, *, reference: datetime | None = None) -> dict[str, dict[str, Any]]:
    reference = reference or datetime.now(timezone.utc)
    snapshot: dict[str, dict[str, Any]] = {}
    if db_path is not None:
        try:
            with connect(db_path) as conn:
                for item in rows(conn, "SELECT name, status_json, updated_at FROM worker_status ORDER BY name"):
                    try:
                        status = json.loads(item["status_json"] or "{}")
                    except json.JSONDecodeError:
                        status = {}
                    status.setdefault("updated_at", item["updated_at"])
                    snapshot[item["name"]] = status
        except sqlite3.Error:
            snapshot = {}
    if not snapshot:
        with WORKER_STATUS_LOCK:
            snapshot = json.loads(json.dumps(WORKER_STATUS))
    for item in snapshot.values():
        item["last_started_age_seconds"] = timestamp_age_seconds(item.get("last_started_at"), reference=reference)
        item["last_finished_age_seconds"] = timestamp_age_seconds(item.get("last_finished_at"), reference=reference)
        item["updated_age_seconds"] = timestamp_age_seconds(item.get("updated_at"), reference=reference)
    return snapshot


def load_inventory(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_db(conn: sqlite3.Connection) -> None:
    ensure_columns(
        conn,
        "users",
        {
            "client_ip": "TEXT",
            "password_salt": "TEXT",
            "password_hash": "TEXT",
        },
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_domain_routes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          domain TEXT NOT NULL UNIQUE,
          server_id TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(server_id) REFERENCES servers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at INTEGER NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_ip_routes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL,
          target_cidr TEXT NOT NULL,
          server_id TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(user_id, target_cidr),
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(server_id) REFERENCES servers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_ip_routes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          target_cidr TEXT NOT NULL UNIQUE,
          server_id TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(server_id) REFERENCES servers(id)
        )
        """
    )
    ensure_columns(
        conn,
        "global_ip_routes",
        {
            "source": "TEXT NOT NULL DEFAULT ''",
            "note": "TEXT NOT NULL DEFAULT ''",
        },
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_candidate_policies (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL DEFAULT '',
          domain TEXT NOT NULL DEFAULT '',
          candidate_server_ids TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(user_id, domain)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_devices (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          display_name TEXT NOT NULL,
          platform TEXT NOT NULL DEFAULT '',
          token_salt TEXT NOT NULL,
          token_hash TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          last_seen_at TEXT,
          last_ip TEXT,
          last_user_agent TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_status (
          device_id TEXT PRIMARY KEY,
          status_json TEXT NOT NULL,
          reported_at TEXT NOT NULL,
          FOREIGN KEY(device_id) REFERENCES agent_devices(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_diagnostics (
          id TEXT PRIMARY KEY,
          device_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          platform TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          report_text TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(device_id) REFERENCES agent_devices(id) ON DELETE CASCADE,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_enrollment_codes (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          desired_device_id TEXT,
          display_name TEXT NOT NULL DEFAULT '',
          platform TEXT NOT NULL DEFAULT 'android',
          code_salt TEXT NOT NULL,
          code_hash TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          expires_at TEXT NOT NULL,
          used_at TEXT,
          used_device_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(used_device_id) REFERENCES agent_devices(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_discovery_queue (
          domain TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'pending',
          source TEXT NOT NULL DEFAULT '',
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          hit_count INTEGER NOT NULL DEFAULT 1,
          user_ids_json TEXT NOT NULL DEFAULT '[]',
          client_ips_json TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transport_configs (
          server_id TEXT PRIMARY KEY,
          transport_type TEXT NOT NULL,
          interface_name TEXT NOT NULL,
          config_json TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          source TEXT NOT NULL DEFAULT '',
          version TEXT NOT NULL DEFAULT '',
          expires_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_probe_jobs (
          id TEXT PRIMARY KEY,
          domain TEXT NOT NULL,
          user_id TEXT NOT NULL DEFAULT '',
          candidate_server_ids TEXT NOT NULL,
          url TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          assigned_device_id TEXT NOT NULL DEFAULT '',
          claimed_by_device_id TEXT NOT NULL DEFAULT '',
          apply_cache INTEGER NOT NULL DEFAULT 1,
          connect_timeout INTEGER NOT NULL DEFAULT 5,
          max_time INTEGER NOT NULL DEFAULT 12,
          priority INTEGER NOT NULL DEFAULT 100,
          attempts INTEGER NOT NULL DEFAULT 0,
          result_json TEXT NOT NULL DEFAULT '{}',
          winner_server_id TEXT,
          score_ms INTEGER,
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          FOREIGN KEY(winner_server_id) REFERENCES servers(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_aliases (
          alias TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          targets_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_service_aliases (
          user_id TEXT NOT NULL,
          alias TEXT NOT NULL,
          label TEXT NOT NULL,
          targets_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(user_id, alias),
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS critical_services (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL DEFAULT '',
          service_key TEXT NOT NULL,
          label TEXT NOT NULL,
          targets_json TEXT NOT NULL,
          success_pattern TEXT NOT NULL DEFAULT '',
          failure_pattern TEXT NOT NULL DEFAULT '',
          routing_enabled INTEGER NOT NULL DEFAULT 0,
          candidate_server_ids TEXT NOT NULL DEFAULT '[]',
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(user_id, service_key)
        )
        """
    )
    ensure_columns(
        conn,
        "critical_services",
        {
            "routing_enabled": "INTEGER NOT NULL DEFAULT 0",
            "candidate_server_ids": "TEXT NOT NULL DEFAULT '[]'",
        },
    )


def init_db(db_path: Path, inventory_path: Path, *, reset_from_inventory: bool = False) -> None:
    inventory = load_inventory(inventory_path)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        seed_inventory(conn, inventory, reset_from_inventory=reset_from_inventory)
        seed_service_aliases(conn)
        seed_managed_global_domain_routes(conn)
        ensure_default_user(conn)


def hash_password(password: str, salt_b64: str | None = None) -> tuple[str, str]:
    salt = base64.b64decode(salt_b64) if salt_b64 else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def verify_password(password: str, salt_b64: str | None, hash_b64: str | None) -> bool:
    if not salt_b64 or not hash_b64:
        return False
    _, expected = hash_password(password, salt_b64)
    return hmac.compare_digest(expected, hash_b64)


def generate_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def generate_enrollment_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return ENROLLMENT_CODE_PREFIX + "-".join(groups)


def hash_device_token(token: str, salt_b64: str | None = None) -> tuple[str, str]:
    return hash_password(token, salt_b64)


def verify_device_token(token: str, salt_b64: str | None, hash_b64: str | None) -> bool:
    if not token or not salt_b64 or not hash_b64:
        return False
    _, expected = hash_device_token(token, salt_b64)
    return hmac.compare_digest(expected, hash_b64)


def normalize_enrollment_code(value: str) -> str:
    code = re.sub(r"\s+", "", value.strip()).upper()
    if code.startswith(ENROLLMENT_CODE_PREFIX.upper()):
        code = ENROLLMENT_CODE_PREFIX + code[len(ENROLLMENT_CODE_PREFIX) :].upper()
    elif code:
        code = ENROLLMENT_CODE_PREFIX + code
    return code


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_device_id(value: str) -> str:
    device_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    device_id = device_id.strip(".-")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,96}", device_id or ""):
        raise ValueError("device id must be 2-96 chars: A-Z a-z 0-9 _ . -")
    return device_id


def normalize_platform(value: str | None) -> str:
    platform = re.sub(r"[^A-Za-z0-9_.+-]+", "-", (value or "").strip().lower())
    return platform[:40]


def normalize_client_ip(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    raw = raw.split(",", 1)[0].strip()
    raw = raw[:-3] if raw.endswith("/32") else raw
    match = re.match(r"^(?:\d{1,3}\.){3}\d{1,3}$", raw)
    if not match:
        raise ValueError("client_ip must be an IPv4 address")
    parts = [int(part) for part in raw.split(".")]
    if any(part > 255 for part in parts):
        raise ValueError("client_ip octets must be 0-255")
    return raw


def normalize_ipv4_cidr(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("target CIDR is required")
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError as exc:
        raise ValueError(f"target must be an IPv4 address or CIDR: {raw}") from exc
    if network.version != 4:
        raise ValueError("target must be IPv4")
    return str(network)


def user_id_from_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip())
    base = re.sub(r"-+", "-", base).strip("-._")
    return base[:64] or "user"


def parse_client_conf(path: Path) -> tuple[str, str] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^Address\s*=\s*([^\s,]+)", text, re.MULTILINE)
    if not match:
        return None
    address = normalize_client_ip(match.group(1))
    if not address:
        return None
    name = path.stem
    for suffix in ("-linux-awg", "-awg", "-linux"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return user_id_from_name(name), address


def normalize_client_ip_or_none(value: str | None) -> str | None:
    try:
        return normalize_client_ip(value)
    except ValueError:
        return None


def create_or_update_user(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    display_name: str,
    role: str,
    password: str | None,
    client_ip: str | None = None,
    enabled: bool = True,
    allow_no_password: bool = False,
) -> None:
    if role not in {"admin", "user"}:
        raise ValueError("role must be admin or user")
    if not user_id or not re.match(r"^[A-Za-z0-9_.-]{2,64}$", user_id):
        raise ValueError("user id must be 2-64 chars: A-Z a-z 0-9 _ . -")
    init_db(db_path, inventory_path)
    timestamp = now()
    salt_hash = hash_password(password) if password is not None else (None, None)
    normalized_client_ip = normalize_client_ip(client_ip)
    with connect(db_path) as conn:
        existing = row(conn, "SELECT id FROM users WHERE id = ?", (user_id,))
        if existing is None:
            if password is None and not allow_no_password:
                raise ValueError("password is required for a new user")
            conn.execute(
                """
                INSERT INTO users (
                  id, display_name, role, default_server_id, client_ip, password_salt,
                  password_hash, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, 'auto', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    display_name,
                    role,
                    normalized_client_ip,
                    salt_hash[0],
                    salt_hash[1],
                    int(enabled),
                    timestamp,
                    timestamp,
                ),
            )
        else:
            if password is None:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, client_ip = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, normalized_client_ip, int(enabled), timestamp, user_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, client_ip = ?, password_salt = ?, password_hash = ?,
                        enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        display_name,
                        role,
                        normalized_client_ip,
                        salt_hash[0],
                        salt_hash[1],
                        int(enabled),
                        timestamp,
                        user_id,
                    ),
                )


def import_cudy_clients(db_path: Path, inventory_path: Path, source_dir: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    imported: list[dict[str, Any]] = []
    seen_addresses: set[str] = set()
    for path in sorted(source_dir.glob("*.conf")):
        parsed = parse_client_conf(path)
        if not parsed:
            continue
        user_id, client_ip = parsed
        if client_ip in seen_addresses:
            continue
        seen_addresses.add(client_ip)
        create_or_update_user(
            db_path,
            inventory_path,
            user_id=user_id,
            display_name=user_id,
            role="user",
            password=None,
            client_ip=client_ip,
            enabled=True,
            allow_no_password=True,
        )
        imported.append({"id": user_id, "client_ip": client_ip, "source": str(path)})
    return imported


def seed_inventory(conn: sqlite3.Connection, inventory: dict[str, Any], *, reset_from_inventory: bool) -> None:
    rows: list[dict[str, Any]] = []
    auto = inventory.get("auto_choice", {})
    rows.append(
        {
            "id": auto.get("id", "auto"),
            "label": auto.get("label", "Auto"),
            "provider": "virtual",
            "kind": auto.get("kind", "virtual"),
            "interface": None,
            "geo_country": "auto",
            "geo_region": None,
            "endpoint": None,
            "switch_command": None,
            "enabled": bool(auto.get("enabled", True)),
            "user_visible": bool(auto.get("user_visible", True)),
            "admin_visible": False,
            "metadata_json": json.dumps(auto, ensure_ascii=False, sort_keys=True),
        }
    )
    rows.append(
        {
            "id": "direct",
            "label": "Direct internet",
            "provider": "virtual",
            "kind": "local",
            "interface": None,
            "geo_country": "direct",
            "geo_region": None,
            "endpoint": None,
            "switch_command": None,
            "enabled": True,
            "user_visible": False,
            "admin_visible": False,
            "metadata_json": json.dumps({"id": "direct", "label": "Direct internet", "kind": "local"}, ensure_ascii=False, sort_keys=True),
        }
    )

    for server in inventory.get("servers", []):
        geo = server.get("geo") or {}
        rows.append(
            {
                "id": server["id"],
                "label": server.get("label", server["id"]),
                "provider": server.get("provider", ""),
                "kind": server.get("kind", ""),
                "interface": server.get("interface"),
                "geo_country": geo.get("country"),
                "geo_region": geo.get("region"),
                "endpoint": server.get("endpoint"),
                "switch_command": server.get("switch_command"),
                "enabled": bool(server.get("enabled", False)),
                "user_visible": bool(server.get("user_visible", False)),
                "admin_visible": bool(server.get("admin_visible", True)),
                "metadata_json": json.dumps(server, ensure_ascii=False, sort_keys=True),
            }
        )

    timestamp = now()
    for sort_order, row in enumerate(rows):
        existing = conn.execute("SELECT id FROM servers WHERE id = ?", (row["id"],)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO servers (
                  id, label, provider, kind, interface, geo_country, geo_region, endpoint,
                  switch_command, enabled, user_visible, admin_visible, sort_order,
                  metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["label"],
                    row["provider"],
                    row["kind"],
                    row["interface"],
                    row["geo_country"],
                    row["geo_region"],
                    row["endpoint"],
                    row["switch_command"],
                    int(row["enabled"]),
                    int(row["user_visible"]),
                    int(row["admin_visible"]),
                    sort_order,
                    row["metadata_json"],
                    timestamp,
                    timestamp,
                ),
            )
        else:
            if reset_from_inventory:
                conn.execute(
                    """
                    UPDATE servers
                    SET label = ?, provider = ?, kind = ?, interface = ?, geo_country = ?,
                        geo_region = ?, endpoint = ?, switch_command = ?, enabled = ?,
                        user_visible = ?, admin_visible = ?, sort_order = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        row["label"],
                        row["provider"],
                        row["kind"],
                        row["interface"],
                        row["geo_country"],
                        row["geo_region"],
                        row["endpoint"],
                        row["switch_command"],
                        int(row["enabled"]),
                        int(row["user_visible"]),
                        int(row["admin_visible"]),
                        sort_order,
                        row["metadata_json"],
                        timestamp,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE servers
                    SET provider = ?, kind = ?, interface = ?, geo_country = ?, geo_region = ?,
                        endpoint = ?, switch_command = ?, admin_visible = ?, sort_order = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        row["provider"],
                        row["kind"],
                        row["interface"],
                        row["geo_country"],
                        row["geo_region"],
                        row["endpoint"],
                        row["switch_command"],
                        int(row["admin_visible"]),
                        sort_order,
                        row["metadata_json"],
                        timestamp,
                        row["id"],
                    ),
                )


def normalize_alias(value: str) -> str:
    alias = re.sub(r"\s+", " ", (value or "").strip().lower())
    if not alias or len(alias) > 80:
        raise ValueError("Alias must be 1-80 characters")
    return alias


def parse_alias_targets(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in re.split(r"[\s,;]+", value) if item.strip()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("targets must be a list or a comma-separated string")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_lookup_target(item)["target"]
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    if not result:
        raise ValueError("Alias targets cannot be empty")
    return result


def seed_service_aliases(conn: sqlite3.Connection) -> None:
    timestamp = now()
    for seed in SERVICE_ALIAS_SEEDS:
        targets_json = json.dumps(seed["targets"], ensure_ascii=False)
        for alias in seed["aliases"]:
            conn.execute(
                """
                INSERT INTO service_aliases (alias, label, targets_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO NOTHING
                """,
                (normalize_alias(alias), seed["label"], targets_json, timestamp, timestamp),
            )


def seed_managed_global_domain_routes(conn: sqlite3.Connection) -> None:
    timestamp = now()
    for seed in MANAGED_GLOBAL_DOMAIN_ROUTE_SEEDS:
        server_id = str(seed.get("server_id") or "auto")
        note = str(seed.get("note") or "Managed global domain route")
        if server_id not in {"auto", "direct"} and row(conn, "SELECT id FROM servers WHERE id = ?", (server_id,)) is None:
            continue
        for domain in seed.get("domains") or []:
            normalized_domain = normalize_domain(str(domain))
            conn.execute(
                """
                INSERT INTO global_domain_routes (domain, server_id, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(domain) DO NOTHING
                """,
                (normalized_domain, server_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO domain_discovery_queue (
                  domain, status, source, first_seen_at, last_seen_at, hit_count,
                  user_ids_json, client_ips_json, note
                ) VALUES (?, 'promoted', 'managed-seed', ?, ?, 1, '[]', '[]', ?)
                ON CONFLICT(domain) DO NOTHING
                """,
                (normalized_domain, timestamp, timestamp, note),
            )


def ensure_default_user(conn: sqlite3.Connection) -> None:
    timestamp = now()
    conn.execute(
        """
        INSERT INTO users (id, display_name, role, default_server_id, enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (DEFAULT_USER_ID, "Default user", "user", "auto", timestamp, timestamp),
    )


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    value = conn.execute(sql, params).fetchone()
    return dict(value) if value is not None else None


PROVIDER_TRANSPORT_PROVIDERS = {"vpntype", "lokvpn"}


def provider_transport_required(server: dict[str, Any]) -> bool:
    return (server.get("provider") or "") in PROVIDER_TRANSPORT_PROVIDERS and (server.get("kind") or "") in {
        "http-proxy-tun",
        "sing-box-profile",
    }


def transport_status_by_server(conn: sqlite3.Connection, *, reference: datetime | None = None) -> dict[str, dict[str, Any]]:
    reference = reference or datetime.now(timezone.utc)
    result: dict[str, dict[str, Any]] = {}
    for item in rows(conn, "SELECT server_id, enabled, updated_at FROM transport_configs"):
        age = timestamp_age_seconds(item.get("updated_at"), reference=reference)
        stale = age is not None and age > TRANSPORT_STALE_WARN_SECONDS
        result[item["server_id"]] = {
            "transport_config_present": True,
            "transport_config_enabled": bool(item.get("enabled")),
            "transport_updated_at": item.get("updated_at"),
            "transport_age_seconds": age,
            "transport_stale": stale,
        }
    return result


def annotate_server_transport_status(conn: sqlite3.Connection, servers: list[dict[str, Any]] | dict[str, dict[str, Any]]) -> list[dict[str, Any]] | dict[str, dict[str, Any]]:
    statuses = transport_status_by_server(conn)
    items = servers.values() if isinstance(servers, dict) else servers
    for server in items:
        status = statuses.get(server.get("id"), {})
        requires_transport = provider_transport_required(server)
        server["transport_required"] = requires_transport
        server["transport_config_present"] = bool(status.get("transport_config_present"))
        server["transport_config_enabled"] = bool(status.get("transport_config_enabled"))
        server["transport_updated_at"] = status.get("transport_updated_at")
        server["transport_age_seconds"] = status.get("transport_age_seconds")
        server["transport_stale"] = bool(status.get("transport_stale"))
        server["candidate_available"] = (
            not requires_transport
            or (
                bool(status.get("transport_config_present"))
                and bool(status.get("transport_config_enabled"))
                and not bool(status.get("transport_stale"))
            )
        )
    return servers


def user_servers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    servers = rows(
        conn,
        """
        SELECT id, label, provider, kind, interface, geo_country, geo_region, enabled, user_visible
        FROM servers
        WHERE enabled = 1 AND user_visible = 1
        ORDER BY sort_order, label
        """,
    )
    return annotate_server_transport_status(conn, servers)


def admin_servers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    servers = rows(
        conn,
        """
        SELECT id, label, provider, kind, interface, geo_country, geo_region, endpoint,
               switch_command, enabled, user_visible, admin_visible, sort_order
        FROM servers
        WHERE admin_visible = 1 OR id = 'auto'
        ORDER BY sort_order, label
        """,
    )
    return annotate_server_transport_status(conn, servers)


def server_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    servers = {
        item["id"]: item
        for item in rows(
            conn,
            """
            SELECT id, label, provider, kind, interface, geo_country, geo_region,
                   endpoint, switch_command, enabled, user_visible, admin_visible,
                   sort_order, metadata_json
            FROM servers
            """,
        )
    }
    return annotate_server_transport_status(conn, servers)


def compact_server(server: dict[str, Any] | None) -> dict[str, Any] | None:
    if not server:
        return None
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(server.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": server.get("id"),
        "label": server.get("label"),
        "provider": server.get("provider"),
        "kind": server.get("kind"),
        "interface": server.get("interface"),
        "profile": metadata.get("profile"),
        "profile_command": metadata.get("profile_command"),
        "switch_command": server.get("switch_command"),
        "geo_country": server.get("geo_country"),
        "geo_region": server.get("geo_region"),
        "enabled": bool(server.get("enabled")),
        "candidate_available": bool(server.get("candidate_available", True)),
        "transport_required": bool(server.get("transport_required")),
        "transport_stale": bool(server.get("transport_stale")),
        "transport_age_seconds": server.get("transport_age_seconds"),
    }


def normalize_transport_type(value: str) -> str:
    transport_type = (value or "").strip()
    allowed = {"amneziawg-conf", "http-proxy-tun", "vless-reality-tun", "sing-box-json"}
    if transport_type not in allowed:
        raise ValueError(f"transport_type must be one of: {', '.join(sorted(allowed))}")
    return transport_type


def normalize_interface_name(value: str | None, *, default: str) -> str:
    interface_name = (value or default or "").strip()
    if not SAFE_INTERFACE_RE.match(interface_name):
        raise ValueError("interface_name must contain only A-Z a-z 0-9 _ . -")
    return interface_name


def transport_config_rows(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE t.enabled = 1" if enabled_only else ""
    entries = rows(
        conn,
        f"""
        SELECT t.server_id, t.transport_type, t.interface_name, t.config_json,
               t.enabled, t.source, t.version, t.expires_at, t.created_at, t.updated_at,
               s.label, s.provider, s.kind, s.enabled AS server_enabled
        FROM transport_configs t
        JOIN servers s ON s.id = t.server_id
        {where}
        ORDER BY s.sort_order, t.server_id
        """,
    )
    for entry in entries:
        try:
            entry["config"] = json.loads(entry.pop("config_json") or "{}")
        except json.JSONDecodeError:
            entry["config"] = {}
    return entries


def transport_endpoint_summary(config: dict[str, Any]) -> str:
    server = str(config.get("server") or "")
    port = config.get("server_port")
    if not server and config.get("endpoint"):
        return str(config.get("endpoint") or "")
    if not server and isinstance(config.get("outbounds"), list):
        for outbound in config["outbounds"]:
            if not isinstance(outbound, dict):
                continue
            if outbound.get("tag") == "proxy-out" or outbound.get("type") in {"http", "socks", "vless"}:
                server = str(outbound.get("server") or "")
                port = outbound.get("server_port")
                if server and port:
                    break
    return f"{server}:{port}" if server and port else ""


def transport_config_summaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in transport_config_rows(conn):
        result.append(
            {
                "server_id": item["server_id"],
                "label": item.get("label") or item["server_id"],
                "provider": item.get("provider") or "",
                "kind": item.get("kind") or "",
                "transport_type": item["transport_type"],
                "interface_name": item["interface_name"],
                "enabled": bool(item["enabled"]),
                "server_enabled": bool(item.get("server_enabled")),
                "active": bool(item["enabled"]) and bool(item.get("server_enabled")),
                "source": item.get("source") or "",
                "version": item.get("version") or "",
                "expires_at": item.get("expires_at"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "endpoint": transport_endpoint_summary(item.get("config") or {}),
            }
        )
    return result


def mark_transport_config_unavailable(
    db_path: Path,
    inventory_path: Path,
    *,
    server_id: str,
    transport_type: str,
    interface_name: str | None = None,
    source: str,
    version: str = "",
    reason: str,
) -> dict[str, Any]:
    return save_transport_config(
        db_path,
        inventory_path,
        server_id=server_id,
        transport_type=transport_type,
        interface_name=interface_name,
        config={"unavailable": True, "reason": reason},
        enabled=False,
        source=source,
        version=version,
    )


def save_transport_config(
    db_path: Path,
    inventory_path: Path,
    *,
    server_id: str,
    transport_type: str,
    interface_name: str | None,
    config: dict[str, Any],
    enabled: bool = True,
    source: str = "",
    version: str = "",
    expires_at: str | None = None,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    transport_type = normalize_transport_type(transport_type)
    if not isinstance(config, dict) or not config:
        raise ValueError("transport config must be a non-empty JSON object")
    timestamp = now()
    with connect(db_path) as conn:
        server = row(conn, "SELECT id, interface FROM servers WHERE id = ?", (server_id,))
        if not server:
            raise ValueError(f"Unknown server_id: {server_id}")
        interface = normalize_interface_name(interface_name, default=server.get("interface") or server_id)
        conn.execute(
            """
            INSERT INTO transport_configs (
              server_id, transport_type, interface_name, config_json, enabled,
              source, version, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
              transport_type = excluded.transport_type,
              interface_name = excluded.interface_name,
              config_json = excluded.config_json,
              enabled = excluded.enabled,
              source = excluded.source,
              version = excluded.version,
              expires_at = excluded.expires_at,
              updated_at = excluded.updated_at
            """,
            (
                server_id,
                transport_type,
                interface,
                json.dumps(config, ensure_ascii=False, sort_keys=True),
                int(enabled),
                source,
                version,
                expires_at,
                timestamp,
                timestamp,
            ),
        )
        saved = row(
            conn,
            """
            SELECT server_id, transport_type, interface_name, config_json,
                   enabled, source, version, expires_at, created_at, updated_at
            FROM transport_configs
            WHERE server_id = ?
            """,
            (server_id,),
        )
    assert saved is not None
    saved["config"] = json.loads(saved.pop("config_json") or "{}")
    return saved


def delete_transport_config(db_path: Path, inventory_path: Path, *, server_id: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        existing = row(conn, "SELECT server_id FROM transport_configs WHERE server_id = ?", (server_id,))
        if not existing:
            raise ValueError(f"Transport config not found: {server_id}")
        conn.execute("DELETE FROM transport_configs WHERE server_id = ?", (server_id,))
    return {"ok": True, "server_id": server_id}


VPNTYPE_PROVIDER_META: dict[str, dict[str, Any]] = {
    "proxygb": {"country": "GB", "candidates": [142, 85]},
    "proxyca": {"country": "CA", "candidates": [143, 82]},
    "proxyfr": {"country": "FR", "candidates": [145, 81]},
    "proxyby": {"country": "BY", "candidates": [146, 80]},
    "proxyae": {"country": "AE", "candidates": [147, 79]},
    "proxyhk": {"country": "HK", "candidates": [148, 78]},
    "proxykz": {"country": "KZ", "candidates": [149, 77]},
    "proxytr": {"country": "TR", "candidates": [150, 76]},
    "proxyil": {"country": "IL", "candidates": [151, 75]},
    "proxycz": {"country": "CZ", "candidates": [152, 74]},
    "proxypl": {"country": "PL", "candidates": [153, 61]},
    "proxyfi": {"country": "FI", "candidates": [154, 60]},
    "proxynl": {"country": "NL", "candidates": [155, 59]},
    "proxyal": {"country": "AL", "candidates": [156, 58]},
    "proxyru": {"country": "RU", "candidates": [157, 57]},
    "proxyus": {"country": "US", "candidates": [158, 56]},
    "proxyde": {"country": "DE", "candidates": [159, 55]},
}

LOKVPN_PROFILE_TAGS: dict[str, list[str]] = {
    "smart1": ["DE", "RU"],
    "de1": ["DE"],
    "ru1": ["RU"],
    "nl1": ["NL"],
    "fr1": ["FR"],
    "se1": ["SE"],
    "smart2": ["RU", "DE"],
    "de2": ["DE"],
    "ru2": ["RU"],
    "nl2": ["NL"],
    "fr2": ["FR"],
    "se2": ["SE"],
}


def read_secret_value(env_names: list[str], file_path: Path) -> str:
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    if file_path.exists():
        return file_path.read_text(encoding="utf-8").strip()
    return ""


def deterministic_tun_address(server_id: str, *, octet2: int) -> str:
    digest = hashlib.sha256(server_id.encode("utf-8")).digest()
    octet3 = 2 + digest[0] % 238
    return f"172.{octet2}.{octet3}.1/30"


def host_direct_route_rule(host: str) -> dict[str, Any]:
    try:
        ipaddress.ip_address(host)
        return {"ip_cidr": [f"{host}/32"], "outbound": "direct"}
    except ValueError:
        return {"domain": [host], "outbound": "direct"}


def make_http_proxy_tun_config(
    *,
    name: str,
    proxy_host: str,
    proxy_port: int,
    proxy_type: str = "http",
    interface_name: str | None = None,
    mtu: int = 1400,
) -> dict[str, Any]:
    if proxy_type not in {"http", "socks"}:
        raise ValueError("proxy_type must be http or socks")
    iface = normalize_interface_name(interface_name, default=name)
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "tun",
                "tag": f"{name}-tun",
                "interface_name": iface,
                "address": [deterministic_tun_address(name, octet2=41)],
                "mtu": mtu,
                "auto_route": False,
                "strict_route": False,
                "stack": "gvisor",
            }
        ],
        "outbounds": [
            {
                "type": proxy_type,
                "tag": "proxy-out",
                "server": proxy_host,
                "server_port": int(proxy_port),
            },
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "auto_detect_interface": True,
            "rules": [host_direct_route_rule(proxy_host)],
            "final": "proxy-out",
        },
    }


def make_vless_reality_tun_config(
    *,
    name: str,
    server: str,
    server_port: int,
    uuid: str,
    flow: str,
    sni: str,
    public_key: str,
    short_id: str,
    interface_name: str | None = None,
    mtu: int = 1400,
) -> dict[str, Any]:
    iface = normalize_interface_name(interface_name, default=name)
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "tun",
                "tag": f"{name}-tun",
                "interface_name": iface,
                "address": [deterministic_tun_address(name, octet2=42)],
                "mtu": mtu,
                "auto_route": False,
                "strict_route": False,
                "stack": "gvisor",
            }
        ],
        "outbounds": [
            {
                "type": "vless",
                "tag": "proxy-out",
                "server": server,
                "server_port": int(server_port),
                "uuid": uuid,
                "flow": flow,
                "tls": {
                    "enabled": True,
                    "server_name": sni,
                    "utls": {"enabled": True, "fingerprint": "chrome"},
                    "reality": {"enabled": True, "public_key": public_key, "short_id": short_id},
                },
            },
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "auto_detect_interface": True,
            "rules": [host_direct_route_rule(server)],
            "final": "proxy-out",
        },
    }


def http_request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    proxy_url: str | None = None,
) -> bytes:
    request = Request(url, data=body, headers=headers or {}, method=method)
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url})) if proxy_url else None
    if opener:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def multipart_form_data(fields: dict[str, Any]) -> tuple[bytes, str]:
    boundary = "----cudy-control-" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def vpntype_post(*, auth: str, url: str, fields: dict[str, Any], timeout: int = 30) -> Any:
    body, content_type = multipart_form_data(fields)
    raw = http_request_bytes(
        url,
        method="POST",
        headers={"Authorization": auth, "Content-Type": content_type},
        body=body,
        timeout=timeout,
    )
    return json.loads(raw.decode("utf-8-sig"))


def vpntype_candidate_ids(list_json: Any, *, country: str, base_candidates: list[int]) -> list[int]:
    ids: list[int] = []
    for candidate in base_candidates:
        if int(candidate) not in ids:
            ids.append(int(candidate))
    if isinstance(list_json, list):
        for item in list_json:
            if not isinstance(item, dict):
                continue
            if str(item.get("country_id") or "") != country or not item.get("id"):
                continue
            candidate_id = int(item["id"])
            if candidate_id not in ids:
                ids.append(candidate_id)
    return ids


def test_http_proxy_endpoint(
    *,
    server: str,
    port: int,
    url: str,
    connect_timeout: int,
    max_time: int,
) -> bool:
    del connect_timeout
    try:
        raw = http_request_bytes(
            url,
            proxy_url=f"http://{server}:{port}",
            timeout=max(3, max_time),
        )
        return bool(raw)
    except Exception:
        return False


def refresh_vpntype_transport(
    db_path: Path,
    inventory_path: Path,
    *,
    server_id: str,
    auth: str = "",
    uuid: str = "",
    proxy_list_json: Any | None = None,
    proxy_check_url: str = "https://ifconfig.me/ip",
    skip_verify: bool = False,
    connect_timeout: int = 5,
    max_time: int = 12,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    auth = auth or read_secret_value(["VPNTYPE_AUTH_DEFAULT"], DEFAULT_VPNTYPE_AUTH_FILE)
    uuid = uuid or read_secret_value(["VPNTYPE_UUID_DEFAULT"], DEFAULT_VPNTYPE_UUID_FILE)
    if not auth or not uuid:
        raise ValueError("VPNtype credentials are not configured. Set env or secrets/vpntype_auth.txt and secrets/vpntype_uuid.txt.")
    meta = VPNTYPE_PROVIDER_META.get(server_id)
    if not meta:
        raise ValueError(f"Unknown VPNtype provider: {server_id}")
    if proxy_list_json is None:
        proxy_list_json = vpntype_post(
            auth=auth,
            url="https://vpntypedev.com/api/chrome/proxy-list",
            fields={"version": "1.1.1", "uuid": uuid},
        )
    candidates = vpntype_candidate_ids(
        proxy_list_json,
        country=str(meta["country"]),
        base_candidates=[int(item) for item in meta["candidates"]],
    )
    selected: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = []
    for candidate_id in candidates:
        try:
            reply = vpntype_post(
                auth=auth,
                url="https://vpntypedev.com/api/chrome/proxy-credentials",
                fields={"version": "1.1.1", "uuid": uuid, "proxy_id": candidate_id},
            )
            credentials = str(reply.get("credentials") or "")
            match = re.match(r"^([^:]+):([0-9]+)$", credentials)
            if not match:
                failures.append({"proxy_id": candidate_id, "error": "bad credentials reply"})
                continue
            server = match.group(1)
            port = int(match.group(2))
            if skip_verify or test_http_proxy_endpoint(
                server=server,
                port=port,
                url=proxy_check_url,
                connect_timeout=connect_timeout,
                max_time=max_time,
            ):
                selected = {"server": server, "port": port, "proxy_id": candidate_id}
                break
            failures.append({"proxy_id": candidate_id, "endpoint": f"{server}:{port}", "error": "verify failed"})
        except Exception as exc:
            failures.append({"proxy_id": candidate_id, "error": str(exc)})
    if not selected:
        raise ValueError(f"No working VPNtype endpoint for {server_id}. Candidates: {','.join(str(x) for x in candidates)}")
    config = {
        "proxy_type": "http",
        "server": str(selected["server"]),
        "server_port": int(selected["port"]),
    }
    saved = save_transport_config(
        db_path,
        inventory_path,
        server_id=server_id,
        transport_type="http-proxy-tun",
        interface_name=server_id,
        config=config,
        enabled=True,
        source="vpntype-api",
        version=f"proxy_id={selected['proxy_id']}",
    )
    return {
        "server_id": server_id,
        "transport_type": "http-proxy-tun",
        "endpoint": f"{selected['server']}:{selected['port']}",
        "proxy_id": selected["proxy_id"],
        "candidate_ids": candidates,
        "failures": failures,
        "updated_at": saved["updated_at"],
    }


def refresh_vpntype_transports(
    db_path: Path,
    inventory_path: Path,
    *,
    server_ids: list[str] | None = None,
    auth: str = "",
    uuid: str = "",
    skip_verify: bool = False,
    connect_timeout: int = 5,
    max_time: int = 12,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        available = [
            item["id"]
            for item in rows(
                conn,
                "SELECT id FROM servers WHERE provider = 'vpntype' AND kind = 'http-proxy-tun' AND enabled = 1 ORDER BY sort_order, id",
            )
        ]
    selected_ids = server_ids or available
    auth = auth or read_secret_value(["VPNTYPE_AUTH_DEFAULT"], DEFAULT_VPNTYPE_AUTH_FILE)
    uuid = uuid or read_secret_value(["VPNTYPE_UUID_DEFAULT"], DEFAULT_VPNTYPE_UUID_FILE)
    refreshed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if not auth or not uuid:
        return {
            "provider": "vpntype",
            "refreshed": [],
            "failed": [{"server_id": server_id, "error": "VPNtype credentials are not configured"} for server_id in selected_ids],
        }
    try:
        proxy_list_json = vpntype_post(
            auth=auth,
            url="https://vpntypedev.com/api/chrome/proxy-list",
            fields={"version": "1.1.1", "uuid": uuid},
        )
    except Exception as exc:
        return {
            "provider": "vpntype",
            "refreshed": [],
            "failed": [{"server_id": server_id, "error": f"proxy-list failed: {exc}"} for server_id in selected_ids],
        }
    for server_id in selected_ids:
        try:
            refreshed.append(
                refresh_vpntype_transport(
                    db_path,
                    inventory_path,
                    server_id=server_id,
                    auth=auth,
                    uuid=uuid,
                    proxy_list_json=proxy_list_json,
                    skip_verify=skip_verify,
                    connect_timeout=connect_timeout,
                    max_time=max_time,
                )
            )
        except Exception as exc:
            failed.append({"server_id": server_id, "error": str(exc)})
    return {"provider": "vpntype", "refreshed": refreshed, "failed": failed}


def fetch_lokvpn_subscription(sub_url: str) -> Any:
    headers = {
        "X-App-Version": "2.7.0",
        "X-Device-Locale": "RU",
        "X-Device-OS": "Windows",
        "X-Device-model": "Ryzen7Pro4750G_x86_64",
        "X-HWID": "3dadf61c-af37-4ea7-a8d3-ce044ce069d7",
        "X-Ver-OS": "11_10.0.26200",
    }
    raw = http_request_bytes(sub_url, headers=headers, timeout=60)
    return json.loads(raw.decode("utf-8-sig"))


def lokvpn_profile_from_server_id(server_id: str) -> str:
    profile = server_id.removeprefix("lokvpn-")
    if profile not in LOKVPN_PROFILE_TAGS:
        raise ValueError(f"Unknown LokVPN profile server_id: {server_id}")
    return profile


def iter_lokvpn_outbounds(subscription: Any) -> Iterable[dict[str, Any]]:
    configs = subscription if isinstance(subscription, list) else [subscription]
    for config in configs:
        if not isinstance(config, dict):
            continue
        outbounds = config.get("outbounds")
        if not isinstance(outbounds, list):
            continue
        for outbound in outbounds:
            if isinstance(outbound, dict):
                yield outbound


def lokvpn_outbound_tag(outbound: dict[str, Any]) -> str:
    return re.sub(r"\s+", "", str(outbound.get("tag") or "")).upper()


class LokvpnProfileUnavailable(ValueError):
    pass


def find_lokvpn_outbound(subscription: Any, profile: str) -> dict[str, Any]:
    wanted = [item.upper() for item in LOKVPN_PROFILE_TAGS[profile]]
    vless_outbounds = [
        outbound
        for outbound in iter_lokvpn_outbounds(subscription)
        if str(outbound.get("protocol") or "").lower() == "vless"
    ]
    by_tag = {lokvpn_outbound_tag(outbound): outbound for outbound in vless_outbounds}
    for tag in wanted:
        if tag in by_tag:
            return by_tag[tag]
    available = ", ".join(sorted(tag for tag in by_tag if tag)) or "none"
    raise LokvpnProfileUnavailable(f"LokVPN profile {profile} is not present in subscription; available tags: {available}")


def parse_lokvpn_outbound(profile: str, outbound: dict[str, Any]) -> dict[str, Any]:
    try:
        vnext = outbound["settings"]["vnext"][0]
        user = vnext["users"][0]
        reality = outbound["streamSettings"]["realitySettings"]
        parsed = {
            "server": vnext["address"],
            "server_port": int(vnext["port"]),
            "uuid": user["id"],
            "flow": user["flow"],
            "sni": reality["serverName"],
            "public_key": reality["publicKey"],
            "short_id": reality["shortId"],
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"Could not parse LokVPN profile {profile}") from exc
    if isinstance(parsed["short_id"], list):
        short_ids = sorted({str(item).strip() for item in parsed["short_id"] if str(item).strip()})
        parsed["short_id"] = short_ids[0] if short_ids else ""
    if any(not parsed[key] for key in ("server", "server_port", "uuid", "flow", "sni", "public_key", "short_id")):
        raise ValueError(f"Could not parse LokVPN profile {profile}")
    return parsed


def stabilize_lokvpn_short_id(existing: dict[str, Any] | None, refreshed: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(existing, dict):
        return refreshed
    try:
        old_short_id = str(existing["tls"]["reality"]["short_id"] or "")
        new_short_id = str(refreshed["tls"]["reality"]["short_id"] or "")
    except (KeyError, TypeError):
        return refreshed
    if not old_short_id or old_short_id == new_short_id:
        return refreshed

    def identity(config: dict[str, Any]) -> dict[str, Any]:
        normalized = json.loads(json.dumps(config, ensure_ascii=False, sort_keys=True))
        reality = ((normalized.get("tls") or {}).get("reality") or {})
        reality.pop("short_id", None)
        return normalized

    if identity(existing) == identity(refreshed):
        refreshed["tls"]["reality"]["short_id"] = old_short_id
    return refreshed


def refresh_lokvpn_transport(
    db_path: Path,
    inventory_path: Path,
    *,
    server_id: str,
    sub_url: str = "",
    subscription: Any | None = None,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    profile = lokvpn_profile_from_server_id(server_id)
    sub_url = sub_url or read_secret_value(["LOKVPN_SUB_URL", "SUB_URL"], DEFAULT_LOKVPN_SUB_URL_FILE)
    if subscription is None:
        if not sub_url:
            raise ValueError("LokVPN subscription URL is not configured. Set LOKVPN_SUB_URL/SUB_URL or secrets/lokvpn_sub_url.txt.")
        subscription = fetch_lokvpn_subscription(sub_url)
    outbound = find_lokvpn_outbound(subscription, profile)
    parsed = parse_lokvpn_outbound(profile, outbound)
    config = {
        "server": str(parsed["server"]),
        "server_port": int(parsed["server_port"]),
        "uuid": str(parsed["uuid"]),
        "flow": str(parsed["flow"]),
        "tls": {
            "enabled": True,
            "server_name": str(parsed["sni"]),
            "utls": {"enabled": True, "fingerprint": "chrome"},
            "reality": {
                "enabled": True,
                "public_key": str(parsed["public_key"]),
                "short_id": str(parsed["short_id"]),
            },
        },
    }
    with connect(db_path) as conn:
        existing = row(
            conn,
            "SELECT transport_type, config_json, enabled, source FROM transport_configs WHERE server_id = ?",
            (server_id,),
        )
    existing_config: dict[str, Any] | None = None
    if (
        existing
        and existing.get("transport_type") == "vless-reality-tun"
        and existing.get("source") == "lokvpn-subscription"
        and existing.get("enabled")
    ):
        try:
            parsed_existing = json.loads(existing.get("config_json") or "{}")
            if isinstance(parsed_existing, dict):
                existing_config = parsed_existing
        except json.JSONDecodeError:
            pass
    config = stabilize_lokvpn_short_id(existing_config, config)
    saved = save_transport_config(
        db_path,
        inventory_path,
        server_id=server_id,
        transport_type="vless-reality-tun",
        interface_name=server_id,
        config=config,
        enabled=True,
        source="lokvpn-subscription",
        version=profile,
    )
    return {
        "server_id": server_id,
        "transport_type": "vless-reality-tun",
        "profile": profile,
        "endpoint": f"{parsed['server']}:{parsed['server_port']}",
        "updated_at": saved["updated_at"],
    }


def refresh_lokvpn_transports(
    db_path: Path,
    inventory_path: Path,
    *,
    server_ids: list[str] | None = None,
    sub_url: str = "",
    subscription: Any | None = None,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        available = [
            item["id"]
            for item in rows(
                conn,
                "SELECT id FROM servers WHERE provider = 'lokvpn' AND kind = 'sing-box-profile' AND enabled = 1 ORDER BY sort_order, id",
            )
        ]
    selected_ids = server_ids or available
    sub_url = sub_url or read_secret_value(["LOKVPN_SUB_URL", "SUB_URL"], DEFAULT_LOKVPN_SUB_URL_FILE)
    refreshed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if subscription is None and not sub_url:
        return {
            "provider": "lokvpn",
            "refreshed": [],
            "failed": [{"server_id": server_id, "error": "LokVPN subscription URL is not configured"} for server_id in selected_ids],
        }
    if subscription is None:
        try:
            subscription = fetch_lokvpn_subscription(sub_url)
        except Exception as exc:
            return {
                "provider": "lokvpn",
                "refreshed": [],
                "failed": [{"server_id": server_id, "error": f"subscription fetch failed: {exc}"} for server_id in selected_ids],
            }
    for server_id in selected_ids:
        try:
            refreshed.append(
                refresh_lokvpn_transport(
                    db_path,
                    inventory_path,
                    server_id=server_id,
                    sub_url=sub_url,
                    subscription=subscription,
                )
            )
        except LokvpnProfileUnavailable as exc:
            profile = lokvpn_profile_from_server_id(server_id)
            saved = mark_transport_config_unavailable(
                db_path,
                inventory_path,
                server_id=server_id,
                transport_type="vless-reality-tun",
                source="lokvpn-subscription",
                version=profile,
                reason=str(exc),
            )
            failed.append({"server_id": server_id, "error": str(exc), "disabled": True, "updated_at": saved["updated_at"]})
        except Exception as exc:
            failed.append({"server_id": server_id, "error": str(exc)})
    return {"provider": "lokvpn", "refreshed": refreshed, "failed": failed}


def refresh_provider_transports(
    db_path: Path,
    inventory_path: Path,
    *,
    provider: str,
    server_ids: list[str] | None = None,
    skip_verify: bool = False,
    connect_timeout: int = 5,
    max_time: int = 12,
) -> dict[str, Any]:
    if provider == "vpntype":
        return refresh_vpntype_transports(
            db_path,
            inventory_path,
            server_ids=server_ids,
            skip_verify=skip_verify,
            connect_timeout=connect_timeout,
            max_time=max_time,
        )
    if provider == "lokvpn":
        return refresh_lokvpn_transports(db_path, inventory_path, server_ids=server_ids)
    if provider == "all":
        vpntype_ids = [item for item in server_ids or [] if item.startswith("proxy")] if server_ids is not None else None
        lokvpn_ids = [item for item in server_ids or [] if item.startswith("lokvpn-")] if server_ids is not None else None
        results = [
            (
                refresh_vpntype_transports(
                    db_path,
                    inventory_path,
                    server_ids=vpntype_ids,
                    skip_verify=skip_verify,
                    connect_timeout=connect_timeout,
                    max_time=max_time,
                )
                if vpntype_ids is None or vpntype_ids
                else {"provider": "vpntype", "refreshed": [], "failed": []}
            ),
            (
                refresh_lokvpn_transports(
                    db_path,
                    inventory_path,
                    server_ids=lokvpn_ids,
                )
                if lokvpn_ids is None or lokvpn_ids
                else {"provider": "lokvpn", "refreshed": [], "failed": []}
            ),
        ]
        return {"provider": "all", "results": results}
    raise ValueError("provider must be vpntype, lokvpn, or all")


def build_transport_plan(
    conn: sqlite3.Connection,
    *,
    server_ids: set[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    configs = {item["server_id"]: item for item in transport_config_rows(conn, enabled_only=True)}
    plan: list[dict[str, Any]] = []
    for server_id in sorted(server_ids):
        if server_id in {"", "auto", "direct"}:
            continue
        config = configs.get(server_id)
        if not config:
            continue
        plan.append(
            {
                "server_id": server_id,
                "transport_type": config["transport_type"],
                "interface_name": config["interface_name"],
                "config": config["config"],
                "source": config.get("source") or "",
                "version": config.get("version") or "",
                "expires_at": config.get("expires_at"),
                "updated_at": config.get("updated_at"),
            }
        )
    return plan


def auto_cache_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        item["domain"]: item
        for item in rows(
            conn,
            """
            SELECT domain, selected_server_id, score_ms, status, checked_at, metadata_json
            FROM domain_auto_cache
            ORDER BY domain
            """,
        )
    }


def parse_candidate_server_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in re.split(r"[\s,;]+", value) if item.strip()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("candidate_server_ids must be a list or a comma-separated string")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.lower() == "auto":
            raise ValueError("Auto candidate list must contain real servers, not auto")
        if item.lower() == AUTO_ALL_REST:
            item = AUTO_ALL_REST
        if item not in seen:
            result.append(item)
            seen.add(item)
    if not result:
        raise ValueError("candidate list cannot be empty")
    return result


def expand_auto_candidate_ids(servers: dict[str, dict[str, Any]], candidates: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for server_id in candidates:
        if server_id == AUTO_ALL_REST:
            for rest_server_id in default_auto_candidate_ids(servers):
                if rest_server_id not in seen:
                    result.append(rest_server_id)
                    seen.add(rest_server_id)
            continue
        if server_id not in seen:
            result.append(server_id)
            seen.add(server_id)
    return result


def auto_candidate_policy_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    entries = rows(
        conn,
        """
        SELECT id, user_id, domain, candidate_server_ids, enabled, updated_at
        FROM auto_candidate_policies
        ORDER BY user_id, domain
        """,
    )
    servers = server_map(conn)
    for entry in entries:
        try:
            entry["candidate_server_ids"] = json.loads(entry["candidate_server_ids"])
        except json.JSONDecodeError:
            entry["candidate_server_ids"] = []
        entry["expanded_candidate_server_ids"] = expand_auto_candidate_ids(servers, entry["candidate_server_ids"])
        entry["scope"] = auto_policy_scope(entry.get("user_id") or "", entry.get("domain") or "")
    return entries


def auto_policy_scope(user_id: str, domain: str) -> str:
    if user_id and domain:
        return "user_domain"
    if user_id:
        return "user_default"
    if domain:
        return "global_domain"
    return "global_default"


def resolve_auto_candidate_policy(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    domain: str,
) -> dict[str, Any] | None:
    candidates = [
        (user_id, domain),
        (user_id, ""),
        ("", domain),
        ("", ""),
    ]
    for candidate_user_id, candidate_domain in candidates:
        item = row(
            conn,
            """
            SELECT id, user_id, domain, candidate_server_ids, enabled, updated_at
            FROM auto_candidate_policies
            WHERE user_id = ? AND domain = ? AND enabled = 1
            """,
            (candidate_user_id, candidate_domain),
        )
        if not item:
            continue
        try:
            server_ids = json.loads(item["candidate_server_ids"])
        except json.JSONDecodeError:
            server_ids = []
        item["candidate_server_ids"] = server_ids
        item["expanded_candidate_server_ids"] = expand_auto_candidate_ids(server_map(conn), server_ids)
        item["scope"] = auto_policy_scope(item.get("user_id") or "", item.get("domain") or "")
        return item
    return None


def save_auto_candidate_policy(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str | None,
    domain: str | None,
    candidate_server_ids: Any,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = (user_id or "").strip()
    normalized_domain = normalize_domain(domain) if domain else ""
    candidates = parse_candidate_server_ids(candidate_server_ids)
    timestamp = now()
    with connect(db_path) as conn:
        if normalized_user_id and row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,)) is None:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        for server_id in candidates:
            if server_id != AUTO_ALL_REST:
                validate_server_id(conn, server_id, require_user_visible=True)
        conn.execute(
            """
            INSERT INTO auto_candidate_policies (
              user_id, domain, candidate_server_ids, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, domain)
            DO UPDATE SET candidate_server_ids = excluded.candidate_server_ids,
                          enabled = excluded.enabled,
                          updated_at = excluded.updated_at
            """,
            (
                normalized_user_id,
                normalized_domain,
                json.dumps(candidates, ensure_ascii=False),
                int(bool(enabled)),
                timestamp,
                timestamp,
            ),
        )
    return {
        "ok": True,
        "user_id": normalized_user_id,
        "domain": normalized_domain,
        "scope": auto_policy_scope(normalized_user_id, normalized_domain),
        "candidate_server_ids": candidates,
        "enabled": bool(enabled),
        "updated_at": timestamp,
    }


def delete_auto_candidate_policy(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str | None,
    domain: str | None,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = (user_id or "").strip()
    normalized_domain = normalize_domain(domain) if domain else ""
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM auto_candidate_policies WHERE user_id = ? AND domain = ?",
            (normalized_user_id, normalized_domain),
        )
    return {
        "ok": True,
        "user_id": normalized_user_id,
        "domain": normalized_domain,
        "scope": auto_policy_scope(normalized_user_id, normalized_domain),
    }


def sync_route_auto_candidate_policy(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str | None,
    domain: str,
    server_id: str,
    candidate_server_ids: Any,
) -> dict[str, Any] | None:
    if server_id != "auto":
        return delete_auto_candidate_policy(
            db_path,
            inventory_path,
            user_id=user_id,
            domain=domain,
        )
    if candidate_server_ids in (None, "", []):
        return delete_auto_candidate_policy(
            db_path,
            inventory_path,
            user_id=user_id,
            domain=domain,
        )
    return save_auto_candidate_policy(
        db_path,
        inventory_path,
        user_id=user_id,
        domain=domain,
        candidate_server_ids=candidate_server_ids,
        enabled=True,
    )


def resolve_route_server(
    *,
    domain: str,
    requested_server_id: str,
    servers: dict[str, dict[str, Any]],
    auto_cache: dict[str, dict[str, Any]],
    auto_policy: dict[str, Any] | None,
    context: str,
    warnings: list[str],
) -> tuple[str | None, dict[str, Any] | None]:
    if requested_server_id != "auto":
        return requested_server_id, None

    raw_candidates = (
        (auto_policy or {}).get("expanded_candidate_server_ids")
        or (auto_policy or {}).get("candidate_server_ids")
        or default_auto_candidate_ids(servers)
    )
    effective_candidates = expand_auto_candidate_ids(servers, list(raw_candidates))

    def candidate_is_available(candidate_server_id: str) -> bool:
        server = servers.get(candidate_server_id)
        return bool(
            candidate_server_id != "auto"
            and server
            and server.get("enabled")
            and server.get("user_visible")
            and server.get("candidate_available", True)
        )

    def first_available_candidate() -> str | None:
        for candidate_server_id in effective_candidates:
            if candidate_is_available(candidate_server_id):
                return candidate_server_id
        return None

    cached = auto_cache.get(domain)
    if not cached or not cached.get("selected_server_id"):
        fallback_server_id = first_available_candidate()
        if auto_policy:
            candidates = ", ".join(auto_policy.get("candidate_server_ids") or [])
            warnings.append(
                f"{context}: Auto has no cached selected server for {domain}; "
                f"candidate policy {auto_policy['scope']}=[{candidates}]; "
                f"using fallback {fallback_server_id or 'none'}"
            )
        else:
            warnings.append(
                f"{context}: Auto has no cached selected server for {domain}; "
                f"using default fallback {fallback_server_id or 'none'}"
            )
        return fallback_server_id, cached

    selected_server_id = str(cached["selected_server_id"])
    if selected_server_id == "auto":
        fallback_server_id = first_available_candidate()
        warnings.append(f"{context}: Auto cache for {domain} points back to auto; using fallback {fallback_server_id or 'none'}")
        return fallback_server_id, cached
    if selected_server_id not in servers:
        fallback_server_id = first_available_candidate()
        warnings.append(
            f"{context}: Auto cache for {domain} points to unknown server {selected_server_id}; "
            f"using fallback {fallback_server_id or 'none'}"
        )
        return fallback_server_id, cached

    if selected_server_id not in effective_candidates:
        fallback_server_id = first_available_candidate()
        scope = (auto_policy or {}).get("scope", "default")
        warnings.append(
            f"{context}: Auto cache for {domain} selects {selected_server_id}, which is outside "
            f"the effective {scope} candidate policy; using fallback {fallback_server_id or 'none'}"
        )
        return fallback_server_id, cached

    if not candidate_is_available(selected_server_id):
        fallback_server_id = first_available_candidate()
        warnings.append(
            f"{context}: Auto cache for {domain} selects unavailable server {selected_server_id}; "
            f"using fallback {fallback_server_id or 'none'}"
        )
        return fallback_server_id, cached

    return selected_server_id, cached


def auto_cache_key_for_ip_route(target_cidr: str) -> str:
    normalized = normalize_ipv4_cidr(target_cidr)
    return "ip-" + normalized.replace(".", "-").replace("/", "-") + ".iproute.local"


def probe_url_from_note(note: str) -> str | None:
    for token in (note or "").replace(",", " ").split():
        if token.startswith("probe="):
            value = token.split("=", 1)[1].strip()
            if value:
                return value
    return None


def default_probe_url_for_cidr(target_cidr: str) -> str:
    network = ipaddress.ip_network(normalize_ipv4_cidr(target_cidr), strict=False)
    if network.num_addresses > 2:
        target = network.network_address + 1
    else:
        target = network.network_address
    return f"tcp://{target}:443"


def ip_route_probe_url(target_cidr: str, note: str = "") -> str:
    explicit_url = probe_url_from_note(note)
    if explicit_url:
        return explicit_url
    normalized_target = normalize_ipv4_cidr(target_cidr)
    if normalized_target in TELEGRAM_CIDRS:
        return TELEGRAM_PROBE_URL
    return default_probe_url_for_cidr(normalized_target)


def save_auto_cache_entry(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    selected_server_id: str,
    score_ms: int | None,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    domain = normalize_domain(domain)
    selected_server_id = selected_server_id.strip()
    if selected_server_id == "auto":
        raise ValueError("Auto cache must point to a real server, not auto")
    if not status or not re.match(r"^[A-Za-z0-9_.-]{1,32}$", status):
        raise ValueError("status must be 1-32 chars: A-Z a-z 0-9 _ . -")
    if score_ms is not None and score_ms < 0:
        raise ValueError("score_ms must be zero or positive")
    timestamp = now()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    with connect(db_path) as conn:
        validate_server_id(conn, selected_server_id, require_user_visible=True)
        conn.execute(
            """
            INSERT INTO domain_auto_cache (
              domain, selected_server_id, score_ms, status, checked_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain)
            DO UPDATE SET selected_server_id = excluded.selected_server_id,
                          score_ms = excluded.score_ms,
                          status = excluded.status,
                          checked_at = excluded.checked_at,
                          metadata_json = excluded.metadata_json
            """,
            (domain, selected_server_id, score_ms, status, timestamp, metadata_json),
        )
    return {
        "ok": True,
        "domain": domain,
        "selected_server_id": selected_server_id,
        "score_ms": score_ms,
        "status": status,
        "checked_at": timestamp,
        "metadata": metadata or {},
    }


def delete_auto_cache_entry(db_path: Path, inventory_path: Path, domain: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    domain = normalize_domain(domain)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM domain_auto_cache WHERE domain = ?", (domain,))
    return {"ok": True, "domain": domain}


def probe_job_row_to_dict(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    for key in ("candidate_server_ids",):
        try:
            result[key] = json.loads(result.get(key) or "[]")
        except json.JSONDecodeError:
            result[key] = []
    try:
        result["result"] = json.loads(result.pop("result_json") or "{}")
    except json.JSONDecodeError:
        result["result"] = {}
    result["apply_cache"] = bool(result.get("apply_cache"))
    return result


def create_probe_job(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    candidate_server_ids: Any,
    user_id: str = "",
    url: str | None = None,
    assigned_device_id: str = "",
    apply_cache: bool = True,
    connect_timeout: int = 5,
    max_time: int = 12,
    priority: int = 100,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_domain = normalize_domain(domain)
    normalized_user_id = (user_id or "").strip()
    assigned_device_id = (assigned_device_id or "").strip()
    candidates = parse_candidate_server_ids(candidate_server_ids)
    if connect_timeout < 1 or max_time < 1:
        raise ValueError("probe timeouts must be positive")
    job_id = "probe_" + secrets.token_urlsafe(18)
    timestamp = now()
    with connect(db_path) as conn:
        if normalized_user_id and row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,)) is None:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        if assigned_device_id:
            assigned_device = row(
                conn,
                "SELECT id, user_id FROM agent_devices WHERE id = ?",
                (assigned_device_id,),
            )
            if assigned_device is None:
                raise ValueError(f"Unknown agent device: {assigned_device_id}")
            if normalized_user_id and assigned_device.get("user_id") != normalized_user_id:
                raise ValueError(
                    f"Agent device {assigned_device_id} does not belong to user {normalized_user_id}"
                )
        candidates = expand_auto_candidate_ids(server_map(conn), candidates)
        for server_id in candidates:
            validate_server_id(conn, server_id, require_user_visible=True)
        conn.execute(
            """
            INSERT INTO agent_probe_jobs (
              id, domain, user_id, candidate_server_ids, url, status,
              assigned_device_id, apply_cache, connect_timeout, max_time,
              priority, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                normalized_domain,
                normalized_user_id,
                json.dumps(candidates, ensure_ascii=False),
                url or None,
                assigned_device_id,
                int(bool(apply_cache)),
                int(connect_timeout),
                int(max_time),
                int(priority),
                timestamp,
                timestamp,
            ),
        )
        saved = row(conn, "SELECT * FROM agent_probe_jobs WHERE id = ?", (job_id,))
    assert saved is not None
    return probe_job_row_to_dict(saved)


def list_probe_jobs(db_path: Path, inventory_path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        entries = rows(
            conn,
            """
            SELECT *
            FROM agent_probe_jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        )
    return [probe_job_row_to_dict(item) for item in entries]


def reset_probe_jobs(
    db_path: Path,
    inventory_path: Path,
    *,
    status: str = "running",
    older_than_seconds: int = 0,
    domain: str = "",
    assigned_device_id: str = "",
    target_status: str = "pending",
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    status = status.strip().lower()
    target_status = target_status.strip().lower()
    if status not in {"pending", "running", "failed", "done"}:
        raise ValueError("status must be one of: pending, running, failed, done")
    if target_status not in {"pending", "failed"}:
        raise ValueError("target-status must be pending or failed")
    filters = ["status = ?"]
    params: list[Any] = [status]
    if domain:
        filters.append("domain = ?")
        params.append(normalize_domain(domain))
    if assigned_device_id:
        filters.append("assigned_device_id = ?")
        params.append(assigned_device_id)
    if older_than_seconds > 0:
        cutoff_epoch = datetime.now(timezone.utc).replace(microsecond=0).timestamp() - older_than_seconds
        cutoff = datetime.fromtimestamp(cutoff_epoch, timezone.utc).replace(microsecond=0).isoformat()
        filters.append("COALESCE(started_at, updated_at, created_at) < ?")
        params.append(cutoff)
    where_sql = " AND ".join(filters)
    timestamp = now()
    with connect(db_path) as conn:
        matched = conn.execute(f"SELECT count(*) FROM agent_probe_jobs WHERE {where_sql}", params).fetchone()[0]
        if target_status == "pending":
            conn.execute(
                f"""
                UPDATE agent_probe_jobs
                SET status = 'pending',
                    claimed_by_device_id = '',
                    started_at = NULL,
                    finished_at = NULL,
                    error = NULL,
                    updated_at = ?
                WHERE {where_sql}
                """,
                [timestamp, *params],
            )
        else:
            conn.execute(
                f"""
                UPDATE agent_probe_jobs
                SET status = 'failed',
                    error = 'reset by operator',
                    finished_at = ?,
                    updated_at = ?
                WHERE {where_sql}
                """,
                [timestamp, timestamp, *params],
            )
    return {
        "ok": True,
        "matched": int(matched),
        "from_status": status,
        "target_status": target_status,
        "older_than_seconds": int(older_than_seconds),
        "domain": domain,
        "assigned_device_id": assigned_device_id,
    }


def claim_agent_probe_jobs(
    db_path: Path,
    inventory_path: Path,
    *,
    device: dict[str, Any],
    limit: int = 2,
) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    timestamp = now()
    max_limit = max(1, min(int(limit), 10))
    with connect(db_path) as conn:
        status_entry = row(
            conn,
            "SELECT status_json FROM agent_status WHERE device_id = ?",
            (device["id"],),
        )
        try:
            device_status = json.loads((status_entry or {}).get("status_json") or "{}")
        except json.JSONDecodeError:
            device_status = {}
        probe_agent = {
            "device_id": device["id"],
            "user_id": device.get("user_id") or "",
            "platform": device.get("platform") or "",
            "status": device_status,
        }
        servers = server_map(conn)
        entries = rows(
            conn,
            """
            SELECT *
            FROM agent_probe_jobs
            WHERE status = 'pending'
              AND (assigned_device_id = '' OR assigned_device_id = ?)
              AND (user_id = '' OR user_id = ?)
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (device["id"], str(device.get("user_id") or ""), max_limit * 10),
        )
        claimed: list[dict[str, Any]] = []
        for item in entries:
            try:
                candidates = json.loads(item.get("candidate_server_ids") or "[]")
            except json.JSONDecodeError:
                candidates = []
            requires_managed_transports = candidates_require_managed_transports(servers, candidates)
            if not agent_can_probe(probe_agent, requires_managed_transports=requires_managed_transports):
                continue
            cursor = conn.execute(
                """
                UPDATE agent_probe_jobs
                SET status = 'running',
                    claimed_by_device_id = ?,
                    attempts = attempts + 1,
                    started_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (device["id"], timestamp, timestamp, item["id"]),
            )
            if cursor.rowcount == 1:
                saved = row(conn, "SELECT * FROM agent_probe_jobs WHERE id = ?", (item["id"],))
                if saved:
                    claimed_job = probe_job_row_to_dict(saved)
                    claimed_job.update(
                        critical_probe_patterns(
                            conn,
                            user_id=str(device.get("user_id") or claimed_job.get("user_id") or ""),
                            domain=str(claimed_job.get("domain") or ""),
                            url=str(claimed_job.get("url") or ""),
                        )
                    )
                    claimed.append(claimed_job)
                    if len(claimed) >= max_limit:
                        break
    return claimed


def complete_agent_probe_job(
    db_path: Path,
    inventory_path: Path,
    *,
    device: dict[str, Any],
    job_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    timestamp = now()
    with connect(db_path) as conn:
        job = row(conn, "SELECT * FROM agent_probe_jobs WHERE id = ?", (job_id,))
        if not job:
            raise ValueError(f"Unknown probe job: {job_id}")
        if job.get("claimed_by_device_id") not in ("", device["id"]):
            raise PermissionError("Probe job is claimed by another device")
        winner = result.get("winner") if isinstance(result.get("winner"), dict) else None
        winner_server_id = str((winner or {}).get("server_id") or "")
        score_ms_raw = (winner or {}).get("time_total_ms") or (winner or {}).get("elapsed_ms")
        score_ms = int(score_ms_raw) if score_ms_raw not in (None, "") else None
        status = "done" if winner_server_id else "failed"
        error = "" if winner_server_id else "no working candidate"
        if winner_server_id:
            validate_server_id(conn, winner_server_id, require_user_visible=True)
        conn.execute(
            """
            UPDATE agent_probe_jobs
            SET status = ?,
                result_json = ?,
                winner_server_id = ?,
                score_ms = ?,
                error = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                winner_server_id or None,
                score_ms,
                error,
                timestamp,
                timestamp,
                job_id,
            ),
        )
        updated = row(conn, "SELECT * FROM agent_probe_jobs WHERE id = ?", (job_id,))
    if winner_server_id and bool(job.get("apply_cache")):
        save_auto_cache_entry(
            db_path,
            inventory_path,
            domain=job["domain"],
            selected_server_id=winner_server_id,
            score_ms=score_ms,
            status="agent_probe",
            metadata={
                "job_id": job_id,
                "device_id": device["id"],
                "user_id": job.get("user_id") or "",
                "url": result.get("url") or job.get("url") or f"https://{job['domain']}/",
                "candidate_server_ids": result.get("candidate_server_ids") or [],
            },
        )
    assert updated is not None
    return probe_job_row_to_dict(updated)


def active_agent_rows(conn: sqlite3.Connection, *, agent_stale_seconds: int) -> list[dict[str, Any]]:
    reference = datetime.now(timezone.utc)
    entries = rows(
        conn,
        """
        SELECT d.id AS device_id, d.user_id, d.display_name, d.platform,
               d.enabled, d.last_seen_at, s.reported_at, s.status_json
        FROM agent_devices d
        LEFT JOIN agent_status s ON s.device_id = d.id
        WHERE d.enabled = 1
        ORDER BY COALESCE(s.reported_at, d.last_seen_at) DESC, d.id
        """,
    )
    result: list[dict[str, Any]] = []
    for entry in entries:
        seen_at = entry.get("reported_at") or entry.get("last_seen_at")
        age = timestamp_age_seconds(seen_at, reference=reference)
        if age is None or age > agent_stale_seconds:
            continue
        try:
            entry["status"] = json.loads(entry.pop("status_json") or "{}")
        except json.JSONDecodeError:
            entry["status"] = {}
        entry["age_seconds"] = age
        result.append(entry)
    return result


def agent_reports_domain(agent: dict[str, Any], *, domain: str, user_id: str) -> bool:
    status = agent.get("status") or {}
    if user_id and agent.get("user_id") != user_id:
        return False
    for route in status.get("domain_routes") or []:
        if str(route.get("domain") or "").lower() == domain:
            return True
    for route in status.get("ip_routes") or []:
        if str(route.get("auto_cache_key") or "").lower() == domain:
            return True
    return False


def agent_can_manage_transports(agent: dict[str, Any]) -> bool:
    status = agent.get("status") or {}
    capabilities = status.get("capabilities") if isinstance(status.get("capabilities"), dict) else {}
    return bool(capabilities.get("can_manage_transports"))


def agent_can_probe(agent: dict[str, Any], *, requires_managed_transports: bool = False) -> bool:
    status = agent.get("status") or {}
    capabilities = status.get("capabilities") if isinstance(status.get("capabilities"), dict) else {}
    if "can_probe" in capabilities:
        can_probe = bool(capabilities.get("can_probe"))
    else:
        platform_name = str(status.get("platform") or agent.get("platform") or "").lower()
        can_probe = platform_name != "android"
    if requires_managed_transports and not agent_can_manage_transports(agent):
        return False
    return can_probe


def candidates_require_managed_transports(servers: dict[str, dict[str, Any]], candidate_server_ids: list[str]) -> bool:
    for server_id in candidate_server_ids:
        server = servers.get(server_id) or {}
        if server.get("transport_required"):
            return True
    return False


def choose_probe_agent(
    agents: list[dict[str, Any]],
    *,
    domain: str,
    user_id: str,
    requires_managed_transports: bool = False,
) -> str:
    probe_agents = [
        agent
        for agent in agents
        if agent_can_probe(agent, requires_managed_transports=requires_managed_transports)
    ]
    for agent in probe_agents:
        if agent_reports_domain(agent, domain=domain, user_id=user_id):
            return str(agent["device_id"])
    if user_id:
        for agent in probe_agents:
            if agent.get("user_id") == user_id:
                return str(agent["device_id"])
    return ""


def auto_probe_domain_rows(
    conn: sqlite3.Connection,
    *,
    active_domain_limit: int = 0,
) -> list[dict[str, Any]]:
    entries = rows(
        conn,
        """
        SELECT '' AS user_id, domain, NULL AS target_cidr, NULL AS note, updated_at, 'global' AS source
        FROM global_domain_routes
        WHERE enabled = 1 AND server_id = 'auto'
        UNION ALL
        SELECT user_id, domain, NULL AS target_cidr, NULL AS note, updated_at, 'user' AS source
        FROM user_domain_routes
        WHERE enabled = 1 AND server_id = 'auto'
        UNION ALL
        SELECT '' AS user_id, target_cidr AS domain, target_cidr, note, updated_at, 'global_ip' AS source
        FROM global_ip_routes
        WHERE enabled = 1 AND server_id = 'auto'
        UNION ALL
        SELECT user_id, target_cidr AS domain, target_cidr, '' AS note, updated_at, 'user_ip' AS source
        FROM user_ip_routes
        WHERE enabled = 1 AND server_id = 'auto'
        ORDER BY domain, user_id
        """,
    )
    for service in critical_service_rows(conn):
        if not service.get("enabled") or not service.get("routing_enabled"):
            continue
        hosts = critical_service_target_hosts(service)
        targets = list(service.get("targets") or [])
        if not hosts or not targets:
            continue
        entries.append(
            {
                "user_id": service.get("user_id") or "",
                "domain": service_auto_cache_key(str(service.get("user_id") or ""), str(service["service_key"])),
                "target_cidr": None,
                "note": "",
                "updated_at": service.get("updated_at") or "",
                "source": "service_group",
                "url": targets[0],
                "agent_domain": hosts[0],
                "candidate_server_ids": list(service.get("candidate_server_ids") or []),
                "service_key": service["service_key"],
            }
        )
    discovery_activity = {
        item["domain"]: item.get("last_seen_at") or ""
        for item in rows(
            conn,
            """
            SELECT domain, last_seen_at
            FROM domain_discovery_queue
            WHERE status = 'promoted'
            """,
        )
    }
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        target_cidr = entry.get("target_cidr") or ""
        if target_cidr:
            entry["domain"] = auto_cache_key_for_ip_route(target_cidr)
            entry["url"] = ip_route_probe_url(target_cidr, entry.get("note") or "")
        key = (entry.get("user_id") or "", entry["domain"])
        if key in seen:
            continue
        seen.add(key)
        entry["last_activity_at"] = max(
            str(entry.get("updated_at") or ""),
            str(discovery_activity.get(str(entry.get("domain") or "")) or ""),
        )
        result.append(entry)
    result.sort(
        key=lambda item: (
            str(item.get("last_activity_at") or ""),
            str(item.get("updated_at") or ""),
            str(item.get("domain") or ""),
            str(item.get("user_id") or ""),
        ),
        reverse=True,
    )
    if active_domain_limit > 0:
        return result[: max(1, int(active_domain_limit))]
    return result


def select_auto_probe_candidates(
    conn: sqlite3.Connection,
    *,
    domain: str,
    candidates: list[str],
    max_candidates: int,
    leader_count: int = 3,
) -> list[str]:
    if max_candidates <= 0 or len(candidates) <= max_candidates:
        return candidates
    leader_count = max(1, min(int(leader_count), max_candidates))
    leaders = candidates[:leader_count]
    rest = [server_id for server_id in candidates[leader_count:] if server_id not in leaders]
    remaining_slots = max_candidates - len(leaders)
    if remaining_slots <= 0 or not rest:
        return leaders[:max_candidates]
    history = row(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM agent_probe_jobs
        WHERE domain = ?
          AND status IN ('done', 'failed')
        """,
        (domain,),
    )
    offset = (int((history or {}).get("count") or 0) * remaining_slots) % len(rest)
    rotated = rest[offset:] + rest[:offset]
    return leaders + rotated[:remaining_slots]


def probe_result_all_unresolvable(result_json: str | None) -> bool:
    try:
        result = json.loads(result_json or "{}")
    except json.JSONDecodeError:
        return False
    checks = result.get("checks") if isinstance(result, dict) else None
    if not isinstance(checks, list) or not checks:
        return False
    return all(
        isinstance(check, dict) and str(check.get("resolve_status") or "") == "resolve_failed"
        for check in checks
    )


def recent_default_probe_is_unresolvable(
    conn: sqlite3.Connection,
    *,
    domain: str,
    reference: datetime,
    retry_seconds: int,
) -> tuple[bool, int | None]:
    """Return true when agents proved that a default apex probe has no DNS target.

    Domain routes also represent suffixes, so an apex without A/AAAA records is
    valid routing input. Retrying https://<apex>/ every worker cycle only creates
    noise; keep a bounded negative cache and periodically retry it.
    """
    latest = row(
        conn,
        """
        SELECT result_json, updated_at
        FROM agent_probe_jobs
        WHERE domain = ?
          AND status = 'failed'
          AND COALESCE(url, '') = ''
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (domain,),
    )
    if not latest:
        return False, None
    age = timestamp_age_seconds(latest.get("updated_at"), reference=reference)
    if age is None or age >= max(0, int(retry_seconds)):
        return False, age
    return probe_result_all_unresolvable(latest.get("result_json")), age


def create_auto_probe_jobs_once(
    db_path: Path,
    inventory_path: Path,
    *,
    cache_ttl_seconds: int = 3600,
    job_stale_seconds: int = 900,
    agent_stale_seconds: int = 600,
    max_jobs: int = 5,
    max_candidates_per_job: int = 4,
    connect_timeout: int = 5,
    max_time: int = 12,
    unresolvable_retry_seconds: int = 86400,
    active_domain_limit: int = 300,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    reference = datetime.now(timezone.utc)
    stale_started_before = (reference.replace(microsecond=0).timestamp() - job_stale_seconds)
    stale_cutoff = datetime.fromtimestamp(stale_started_before, timezone.utc).replace(microsecond=0).isoformat()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    job_requests: list[dict[str, Any]] = []
    invalid_assignments: list[dict[str, Any]] = []
    active_agent_count = 0
    total_auto_domains = 0
    active_auto_domains = 0
    planned_domains: set[str] = set()
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE agent_probe_jobs
            SET status = 'pending',
                claimed_by_device_id = '',
                started_at = NULL,
                updated_at = ?
            WHERE status = 'running'
              AND COALESCE(started_at, updated_at) < ?
            """,
            (now(), stale_cutoff),
        )
        servers = server_map(conn)
        default_candidates = default_auto_candidate_ids(servers)
        agents = active_agent_rows(conn, agent_stale_seconds=agent_stale_seconds)
        active_agent_count = len(agents)
        agents_by_id = {str(agent.get("device_id") or ""): agent for agent in agents}
        for pending in rows(
            conn,
            """
            SELECT id, domain, assigned_device_id, candidate_server_ids
            FROM agent_probe_jobs
            WHERE status = 'pending' AND assigned_device_id != ''
            """,
        ):
            try:
                pending_candidates = json.loads(pending.get("candidate_server_ids") or "[]")
            except json.JSONDecodeError:
                pending_candidates = []
            assigned_agent = agents_by_id.get(str(pending.get("assigned_device_id") or ""))
            requires_managed_transports = candidates_require_managed_transports(servers, pending_candidates)
            if assigned_agent and agent_can_probe(
                assigned_agent,
                requires_managed_transports=requires_managed_transports,
            ):
                continue
            reason = "assigned agent is offline or no longer probe-capable"
            conn.execute(
                """
                UPDATE agent_probe_jobs
                SET status = 'failed', error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reason, now(), now(), pending["id"]),
            )
            invalid_assignments.append(
                {
                    "id": pending["id"],
                    "domain": pending["domain"],
                    "assigned_device_id": pending["assigned_device_id"],
                    "reason": reason,
                }
            )
        cache = auto_cache_map(conn)
        all_specs = auto_probe_domain_rows(conn)
        total_auto_domains = len(all_specs)
        if active_domain_limit > 0:
            active_specs = all_specs[: max(1, int(active_domain_limit))]
        else:
            active_specs = all_specs
        active_auto_domains = len(active_specs)
        for spec in active_specs:
            if len(job_requests) >= max_jobs:
                break
            domain = spec["domain"]
            user_id = spec.get("user_id") or ""
            if domain in planned_domains:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "already_planned"})
                continue
            existing_job = row(
                conn,
                """
                SELECT id, status
                FROM agent_probe_jobs
                WHERE domain = ? AND status IN ('pending', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (domain,),
            )
            if existing_job:
                skipped.append({"domain": domain, "user_id": user_id, "reason": f"job_{existing_job['status']}", "job_id": existing_job["id"]})
                continue
            cached = cache.get(domain)
            cached_age = timestamp_age_seconds((cached or {}).get("checked_at"), reference=reference)
            if cached and cached.get("selected_server_id") and cached_age is not None and cached_age < cache_ttl_seconds:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "cache_fresh", "age_seconds": cached_age})
                continue
            if not spec.get("url"):
                unresolvable, unresolvable_age = recent_default_probe_is_unresolvable(
                    conn,
                    domain=domain,
                    reference=reference,
                    retry_seconds=unresolvable_retry_seconds,
                )
                if unresolvable:
                    skipped.append(
                        {
                            "domain": domain,
                            "user_id": user_id,
                            "reason": "probe_target_unresolvable",
                            "age_seconds": unresolvable_age,
                            "retry_seconds": int(unresolvable_retry_seconds),
                        }
                    )
                    continue
            spec_candidates = list(spec.get("candidate_server_ids") or [])
            policy = None
            if spec_candidates:
                policy = {
                    "candidate_server_ids": spec_candidates,
                    "expanded_candidate_server_ids": expand_auto_candidate_ids(servers, spec_candidates),
                    "scope": "user_service_group" if user_id else "global_service_group",
                }
            else:
                policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=domain)
            candidates = list(
                (policy or {}).get("expanded_candidate_server_ids")
                or (policy or {}).get("candidate_server_ids")
                or default_candidates
            )
            candidates = expand_auto_candidate_ids(servers, candidates)
            candidates = [
                server_id
                for server_id in candidates
                if server_id in servers
                and servers[server_id].get("enabled")
                and servers[server_id].get("user_visible")
                and servers[server_id].get("candidate_available", True)
            ]
            if not candidates:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "no_candidates"})
                continue
            probe_candidates = select_auto_probe_candidates(
                conn,
                domain=domain,
                candidates=candidates,
                max_candidates=max(1, int(max_candidates_per_job)),
            )
            assigned_device_id = choose_probe_agent(
                agents,
                domain=str(spec.get("agent_domain") or domain),
                user_id=user_id,
                requires_managed_transports=candidates_require_managed_transports(servers, probe_candidates),
            )
            eligible_agents = [
                agent
                for agent in agents
                if agent_can_probe(
                    agent,
                    requires_managed_transports=candidates_require_managed_transports(servers, probe_candidates),
                )
            ]
            if not assigned_device_id and not agents:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "no_active_agent"})
                continue
            if not assigned_device_id and not eligible_agents:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "no_capable_agent"})
                continue
            if user_id and not assigned_device_id:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "no_active_user_agent"})
                continue
            job_requests.append(
                {
                    "domain": domain,
                    "url": spec.get("url") or None,
                    "candidate_server_ids": probe_candidates,
                    "user_id": user_id,
                    "assigned_device_id": assigned_device_id,
                    "expanded_candidate_count": len(candidates),
                }
            )
            planned_domains.add(domain)
    for request in job_requests:
        created.append(
            create_probe_job(
                db_path,
                inventory_path,
                domain=request["domain"],
                candidate_server_ids=request["candidate_server_ids"],
                user_id=request["user_id"],
                url=request.get("url"),
                assigned_device_id=request["assigned_device_id"],
                apply_cache=True,
                connect_timeout=connect_timeout,
                max_time=max_time,
                priority=100,
            )
        )
    return {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "active_agents": active_agent_count,
        "cache_ttl_seconds": cache_ttl_seconds,
        "max_jobs": max_jobs,
        "max_candidates_per_job": max_candidates_per_job,
        "unresolvable_retry_seconds": int(unresolvable_retry_seconds),
        "active_domain_limit": int(active_domain_limit),
        "active_auto_domains": active_auto_domains,
        "total_auto_domains": total_auto_domains,
        "invalid_assignments": invalid_assignments,
    }


def enqueue_auto_probe_for_domain(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    user_id: str = "",
    candidate_server_ids: Any = None,
    max_candidates: int = 4,
    connect_timeout: int = 5,
    max_time: int = 12,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_domain = normalize_domain(domain)
    normalized_user_id = (user_id or "").strip()
    with connect(db_path) as conn:
        existing_job = row(
            conn,
            """
            SELECT id, status
            FROM agent_probe_jobs
            WHERE domain = ? AND status IN ('pending', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized_domain,),
        )
        if existing_job:
            return {
                "ok": True,
                "created": None,
                "skipped": {
                    "domain": normalized_domain,
                    "user_id": normalized_user_id,
                    "reason": f"job_{existing_job['status']}",
                    "job_id": existing_job["id"],
                },
            }
        servers = server_map(conn)
        if candidate_server_ids not in (None, "", []):
            candidates = parse_candidate_server_ids(candidate_server_ids)
        else:
            policy = resolve_auto_candidate_policy(conn, user_id=normalized_user_id, domain=normalized_domain)
            candidates = list(
                (policy or {}).get("expanded_candidate_server_ids")
                or (policy or {}).get("candidate_server_ids")
                or default_auto_candidate_ids(servers)
            )
        candidates = expand_auto_candidate_ids(servers, candidates)
        candidates = [
            server_id
            for server_id in candidates
            if server_id in servers
            and servers[server_id].get("enabled")
            and servers[server_id].get("user_visible")
            and servers[server_id].get("candidate_available", True)
        ]
        if not candidates:
            return {
                "ok": True,
                "created": None,
                "skipped": {
                    "domain": normalized_domain,
                    "user_id": normalized_user_id,
                    "reason": "no_candidates",
                },
            }
        probe_candidates = select_auto_probe_candidates(
            conn,
            domain=normalized_domain,
            candidates=candidates,
            max_candidates=max(1, min(int(max_candidates), 50)),
        )
    created = create_probe_job(
        db_path,
        inventory_path,
        domain=normalized_domain,
        user_id=normalized_user_id,
        candidate_server_ids=probe_candidates,
        apply_cache=True,
        connect_timeout=max(1, min(int(connect_timeout), 60)),
        max_time=max(1, min(int(max_time), 120)),
        priority=50,
    )
    return {"ok": True, "created": created, "skipped": None}


def auto_probe_worker_loop(
    *,
    db_path: Path,
    inventory_path: Path,
    stop_event: threading.Event,
    interval_seconds: int,
    cache_ttl_seconds: int,
    job_stale_seconds: int,
    agent_stale_seconds: int,
    max_jobs: int,
    max_candidates_per_job: int,
    connect_timeout: int,
    max_time: int,
    active_domain_limit: int,
) -> None:
    update_worker_status(
        "auto_probe",
        db_path=db_path,
        enabled=True,
        interval_seconds=interval_seconds,
        last_error=None,
        last_result=None,
    )
    while not stop_event.wait(interval_seconds):
        update_worker_status("auto_probe", db_path=db_path, started=True)
        try:
            result = create_auto_probe_jobs_once(
                db_path,
                inventory_path,
                cache_ttl_seconds=cache_ttl_seconds,
                job_stale_seconds=job_stale_seconds,
                agent_stale_seconds=agent_stale_seconds,
                max_jobs=max_jobs,
                max_candidates_per_job=max_candidates_per_job,
                connect_timeout=connect_timeout,
                max_time=max_time,
                active_domain_limit=active_domain_limit,
            )
            created_count = len(result.get("created") or [])
            update_worker_status(
                "auto_probe",
                db_path=db_path,
                finished=True,
                last_error=None,
                last_result={
                    "created": created_count,
                    "skipped": len(result.get("skipped") or []),
                    "active_agents": result.get("active_agents"),
                    "active_auto_domains": result.get("active_auto_domains"),
                    "total_auto_domains": result.get("total_auto_domains"),
                    "active_domain_limit": result.get("active_domain_limit"),
                },
            )
            if created_count:
                print(f"auto-probe worker: created {created_count} job(s)", file=sys.stderr)
        except Exception as exc:
            update_worker_status("auto_probe", db_path=db_path, finished=True, last_error=str(exc))
            print(f"auto-probe worker failed: {exc}", file=sys.stderr)


def provider_refresh_worker_loop(
    *,
    db_path: Path,
    inventory_path: Path,
    stop_event: threading.Event,
    interval_seconds: int,
    provider: str,
    skip_verify: bool,
    connect_timeout: int,
    max_time: int,
) -> None:
    update_worker_status(
        "provider_refresh",
        db_path=db_path,
        enabled=True,
        interval_seconds=interval_seconds,
        provider=provider,
        last_error=None,
        last_result=None,
    )
    while not stop_event.is_set():
        update_worker_status("provider_refresh", db_path=db_path, started=True)
        try:
            result = refresh_provider_transports(
                db_path,
                inventory_path,
                provider=provider,
                skip_verify=skip_verify,
                connect_timeout=connect_timeout,
                max_time=max_time,
            )
            refreshed = 0
            failed = 0
            if result.get("provider") == "all":
                for item in result.get("results") or []:
                    refreshed += len(item.get("refreshed") or [])
                    failed += len(item.get("failed") or [])
            else:
                refreshed = len(result.get("refreshed") or [])
                failed = len(result.get("failed") or [])
            update_worker_status(
                "provider_refresh",
                db_path=db_path,
                finished=True,
                last_error=None,
                last_result={
                    "refreshed": refreshed,
                    "failed": failed,
                    "provider": provider,
                },
            )
            if refreshed or failed:
                print(f"provider-refresh worker: refreshed={refreshed} failed={failed}", file=sys.stderr)
        except Exception as exc:
            update_worker_status("provider_refresh", db_path=db_path, finished=True, last_error=str(exc))
            print(f"provider-refresh worker failed: {exc}", file=sys.stderr)
        if stop_event.wait(interval_seconds):
            break


def server_metadata(server: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(server.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        return {}


def default_auto_candidate_ids(servers: dict[str, dict[str, Any]]) -> list[str]:
    return [
        server_id
        for server_id, server in sorted(servers.items(), key=lambda item: (item[1].get("sort_order") or 0, item[0]))
        if server_id != "auto" and server.get("enabled") and server.get("user_visible") and server.get("candidate_available", True)
    ]


def parse_curl_probe_output(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    try:
        result["rc"] = int(result.get("rc", "1"))
    except ValueError:
        result["rc"] = 1
    try:
        result["http_code_int"] = int(result.get("http_code", "0"))
    except ValueError:
        result["http_code_int"] = 0
    try:
        result["time_total_ms"] = int(float(result.get("time_total", "0")) * 1000)
    except ValueError:
        result["time_total_ms"] = None
    try:
        result["speed_mbps"] = round(float(result.get("speed_download") or "0") * 8 / 1_000_000, 2)
    except ValueError:
        result["speed_mbps"] = None
    return result


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


def apply_semantic_probe_check(parsed: dict[str, Any], *, body_text: str) -> None:
    evidence = body_geo_block_evidence(body_text)
    http_code = int(parsed.get("http_code_int") or 0)
    try:
        curl_rc = int(parsed.get("rc", 1))
    except (TypeError, ValueError):
        curl_rc = 1
    parsed["ok"] = curl_rc == 0 and 200 <= http_code < 500
    if evidence:
        parsed["semantic_status"] = "geo_blocked"
        parsed["semantic_evidence"] = evidence
        parsed["ok"] = False
    elif parsed["ok"]:
        parsed["semantic_status"] = "ok"
    elif curl_rc != 0:
        parsed["semantic_status"] = "curl_error"
    else:
        parsed["semantic_status"] = "http_error"


def run_cudy_curl_probe(
    client: paramiko.SSHClient,
    *,
    iface: str,
    url: str,
    connect_timeout: int,
    max_time: int,
    timeout: int,
) -> dict[str, Any]:
    command = (
        "body=$(mktemp /tmp/cudy-probe.XXXXXX); "
        "trap 'rm -f \"$body\"' EXIT; "
        "out=$(curl -4 -L -sS -o \"$body\" "
        f"--interface {shlex.quote(iface)} "
        f"--connect-timeout {int(connect_timeout)} --max-time {int(max_time)} "
        "-w 'http_code=%{http_code}\\ntime_total=%{time_total}\\nremote_ip=%{remote_ip}\\n"
        "size_download=%{size_download}\\nspeed_download=%{speed_download}\\n' "
        f"{shlex.quote(url)} 2>&1); "
        f"rc=$?; printf 'rc=%s\\n%s\\n__CUDY_PROBE_BODY__\\n' \"$rc\" \"$out\"; "
        f"head -c {PROBE_BODY_LIMIT_BYTES} \"$body\""
    )
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    stdout.channel.recv_exit_status()
    combined = out + ("\n" + err if err.strip() else "")
    marker = "__CUDY_PROBE_BODY__\n"
    if marker in combined:
        metrics, body_text = combined.split(marker, 1)
    else:
        metrics, body_text = combined, ""
    parsed = parse_curl_probe_output(metrics)
    apply_semantic_probe_check(parsed, body_text=body_text)
    parsed["raw"] = metrics.strip()
    return parsed


def auto_select_domain(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    user_id: str = "",
    candidate_server_ids: Any = None,
    url: str | None = None,
    apply: bool = False,
    deploy: bool = False,
    switch_profiles: bool = False,
    ssh_host: str = DEFAULT_CUDY_HOST,
    ssh_user: str = DEFAULT_CUDY_USER,
    ssh_password: str | None = None,
    ssh_timeout: int = 60,
    connect_timeout: int = 5,
    max_time: int = 12,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_domain = normalize_domain(domain)
    normalized_user_id = (user_id or "").strip()
    probe_url = url or f"https://{normalized_domain}/"

    with connect(db_path) as conn:
        servers = server_map(conn)
        if normalized_user_id and row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,)) is None:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        policy = None
        if candidate_server_ids:
            candidates = parse_candidate_server_ids(candidate_server_ids)
            policy_source = "override"
        else:
            policy = resolve_auto_candidate_policy(conn, user_id=normalized_user_id, domain=normalized_domain)
            if policy:
                candidates = list(policy.get("candidate_server_ids") or [])
                policy_source = policy["scope"]
            else:
                candidates = default_auto_candidate_ids(servers)
                policy_source = "all_enabled_user_visible"
        candidates = expand_auto_candidate_ids(servers, candidates)
        for server_id in candidates:
            validate_server_id(conn, server_id, require_user_visible=True)

    password = load_cudy_ssh_password(ssh_password)
    if not password:
        raise ValueError("Cudy SSH password is not configured. Set CUDY_SSH_PASSWORD or create secrets/cudy_ssh_password.txt")

    checks: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    client = ssh_connect(ssh_host, ssh_user, password, ssh_timeout)
    try:
        for index, server_id in enumerate(candidates, start=1):
            server = servers[server_id]
            metadata = server_metadata(server)
            iface = safe_interface_name(server.get("interface"))
            check: dict[str, Any] = {
                "server_id": server_id,
                "label": server.get("label"),
                "index": index,
                "interface": iface,
                "provider": server.get("provider"),
                "kind": server.get("kind"),
                "ok": False,
            }
            if not iface:
                check["status"] = "no_interface"
                checks.append(check)
                continue
            profile_command = metadata.get("profile_command")
            if profile_command:
                check["profile_command"] = profile_command
                if not switch_profiles:
                    check["status"] = "profile_switch_required"
                    check["error"] = "use --switch-profiles to test LokVPN profile candidates"
                    checks.append(check)
                    continue
                ssh_exec_checked(client, str(profile_command), ssh_timeout)
                check["profile_switched"] = True
                time.sleep(1)
            probe = run_cudy_curl_probe(
                client,
                iface=iface,
                url=probe_url,
                connect_timeout=connect_timeout,
                max_time=max_time,
                timeout=max(ssh_timeout, max_time + 5),
            )
            http_code = int(probe.get("http_code_int") or 0)
            ok = bool(probe.get("ok"))
            check.update(
                {
                    "ok": ok,
                    "status": "ok" if ok else str(probe.get("semantic_status") or "failed"),
                    "http_code": http_code,
                    "score_ms": probe.get("time_total_ms"),
                    "remote_ip": probe.get("remote_ip"),
                    "curl_rc": probe.get("rc"),
                    "semantic_status": probe.get("semantic_status"),
                    "semantic_evidence": probe.get("semantic_evidence"),
                    "raw": probe.get("raw"),
                }
            )
            checks.append(check)
            if ok and (winner is None or (check.get("score_ms") or 10**9) < (winner.get("score_ms") or 10**9)):
                winner = check
    finally:
        client.close()

    saved = None
    deploy_result = None
    if winner and apply:
        saved = save_auto_cache_entry(
            db_path,
            inventory_path,
            domain=normalized_domain,
            selected_server_id=str(winner["server_id"]),
            score_ms=winner.get("score_ms"),
            status="auto",
            metadata={
                "policy_source": policy_source,
                "user_id": normalized_user_id,
                "url": probe_url,
                "checked_candidates": len(checks),
                "switch_profiles": switch_profiles,
            },
        )
        if deploy:
            deploy_result = apply_combined_route_deploy(
                db_path,
                inventory_path,
                ssh_host=ssh_host,
                ssh_user=ssh_user,
                ssh_password=password,
                restart_pbr=True,
                run_user_apply=True,
            )

    return {
        "ok": bool(winner),
        "domain": normalized_domain,
        "user_id": normalized_user_id,
        "url": probe_url,
        "policy_source": policy_source,
        "candidate_server_ids": candidates,
        "winner": winner,
        "checks": checks,
        "applied": bool(saved),
        "saved": saved,
        "deployed": bool(deploy_result),
        "deploy_result": deploy_result,
    }


def validate_client_name(name: str) -> str:
    value = name.strip()
    if not SAFE_CLIENT_NAME_RE.fullmatch(value):
        raise ValueError("client name must be 2-64 chars: A-Z a-z 0-9 _ . -")
    return value


def cudy_client_config_path(client_name: str) -> Path:
    name = validate_client_name(client_name)
    filename = f"{name}.conf" if name.endswith("-awg") else f"{name}-awg.conf"
    return CUDY_CLIENT_OUTPUT_DIR / filename


def cudy_client_config_candidates(client_name: str) -> list[Path]:
    name = validate_client_name(client_name)
    return [
        CUDY_CLIENT_OUTPUT_DIR / f"{name}-awg.conf",
        CUDY_CLIENT_OUTPUT_DIR / f"{name}.conf",
    ]


def find_cudy_client_config(client_name: str) -> Path | None:
    for path in cudy_client_config_candidates(client_name):
        if path.exists() and path.is_file():
            return path
    return None


def parse_config_address(conf: str) -> str | None:
    match = re.search(r"^Address\s*=\s*([^\s,]+)", conf, re.MULTILINE)
    if not match:
        return None
    return normalize_client_ip(match.group(1))


def parse_cudy_friend_list(output: str) -> list[dict[str, str]]:
    friends: list[dict[str, str]] = []
    for index, line in enumerate(output.splitlines()):
        if index == 0 or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        friends.append(
            {
                "name": parts[0],
                "ip": parts[1],
                "enabled": parts[2],
                "endpoint": parts[3],
                "handshake": parts[4],
                "from_peer_bytes": parts[5],
                "to_peer_bytes": parts[6],
            }
        )
    return friends


def sync_cudy_clients_from_router(
    db_path: Path,
    inventory_path: Path,
    *,
    fetch_configs: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    password = load_cudy_ssh_password()
    if not password:
        raise ValueError(
            "Cudy SSH password is not configured. Set CUDY_SSH_PASSWORD or create secrets/cudy_ssh_password.txt"
        )
    client = ssh_connect(DEFAULT_CUDY_HOST, DEFAULT_CUDY_USER, password, 60)
    synced: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        friends = parse_cudy_friend_list(ssh_exec_checked(client, "/usr/bin/friendctl list", 60))
        for friend in friends:
            name = validate_client_name(friend["name"])
            client_ip = normalize_client_ip(friend["ip"])
            enabled = friend["enabled"].lower() == "yes"
            config_path = None
            if fetch_configs:
                try:
                    conf = ssh_exec_checked(client, f"/usr/bin/friendctl conf {shlex.quote(name)}", 60)
                    path = cudy_client_config_path(name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(conf, encoding="ascii", newline="\n")
                    config_path = str(path)
                except RuntimeError as exc:
                    warnings.append(f"{name}: could not fetch config: {exc}")
            create_or_update_user(
                db_path,
                inventory_path,
                user_id=name,
                display_name=name,
                role="user",
                password=None,
                client_ip=client_ip,
                enabled=enabled,
                allow_no_password=True,
            )
            synced.append(
                {
                    **friend,
                    "user_id": name,
                    "client_ip": client_ip,
                    "config_path": config_path,
                }
            )
    finally:
        client.close()
    return {"ok": True, "synced": synced, "warnings": warnings}


def create_cudy_vpn_client(
    *,
    client_name: str,
    endpoint: str = DEFAULT_CUDY_FRIEND_ENDPOINT,
    force: bool = False,
) -> dict[str, Any]:
    name = validate_client_name(client_name)
    if not re.fullmatch(r"[^:\s]+:\d{1,5}", endpoint):
        raise ValueError("endpoint must look like host:port")
    output_path = cudy_client_config_path(name)
    if output_path.exists() and not force:
        raise ValueError(f"client config already exists: {output_path}")
    password = load_cudy_ssh_password()
    if not password:
        raise ValueError(
            "Cudy SSH password is not configured. Set CUDY_SSH_PASSWORD or create secrets/cudy_ssh_password.txt"
        )
    client = ssh_connect(DEFAULT_CUDY_HOST, DEFAULT_CUDY_USER, password, 60)
    try:
        add_output = ssh_exec_checked(
            client,
            f"/usr/bin/friendctl add {shlex.quote(name)} {shlex.quote(endpoint)}",
            180,
        )
        conf = ssh_exec_checked(client, f"/usr/bin/friendctl conf {shlex.quote(name)}", 60)
    finally:
        client.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(conf, encoding="ascii", newline="\n")
    return {
        "client_name": name,
        "client_ip": parse_config_address(conf),
        "config_path": str(output_path),
        "config_download_url": f"/api/admin/client-config?user_id={name}",
        "output": add_output.strip(),
    }


def revoke_cudy_vpn_client(client_name: str) -> dict[str, Any]:
    name = validate_client_name(client_name)
    password = load_cudy_ssh_password()
    if not password:
        raise ValueError(
            "Cudy SSH password is not configured. Set CUDY_SSH_PASSWORD or create secrets/cudy_ssh_password.txt"
        )
    output = ""
    warning = None
    client = ssh_connect(DEFAULT_CUDY_HOST, DEFAULT_CUDY_USER, password, 60)
    try:
        try:
            output = ssh_exec_checked(client, f"/usr/bin/friendctl revoke {shlex.quote(name)}", 120).strip()
        except RuntimeError as exc:
            if "No such peer" in str(exc):
                warning = f"Cudy peer not found: {name}"
            else:
                raise
    finally:
        client.close()
    removed_files: list[str] = []
    for path in cudy_client_config_candidates(name):
        if path.exists() and path.is_file():
            path.unlink()
            removed_files.append(str(path))
    return {"client_name": name, "output": output, "warning": warning, "removed_files": removed_files}


def delete_admin_user(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    revoke_cudy: bool,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("user id is required")
    revoke_result = None
    with connect(db_path) as conn:
        user = row(conn, "SELECT id, role FROM users WHERE id = ?", (user_id,))
        if not user:
            raise ValueError(f"Unknown user: {user_id}")
        if user["role"] == "admin":
            admin_count = conn.execute("SELECT count(*) FROM users WHERE role = 'admin' AND enabled = 1").fetchone()[0]
            if admin_count <= 1:
                raise ValueError("Cannot delete the last enabled admin")
    if revoke_cudy:
        revoke_result = revoke_cudy_vpn_client(user_id)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM auto_candidate_policies WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if not revoke_cudy:
        removed_files = []
        for path in cudy_client_config_candidates(user_id):
            if path.exists() and path.is_file():
                path.unlink()
                removed_files.append(str(path))
        revoke_result = {"removed_files": removed_files}
    return {"ok": True, "deleted_user_id": user_id, "revoke_cudy": bool(revoke_cudy), "revoke": revoke_result}


def create_agent_device(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    device_id: str | None = None,
    display_name: str | None = None,
    platform: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = user_id.strip()
    normalized_platform = normalize_platform(platform)
    if device_id:
        normalized_device_id = normalize_device_id(device_id)
    else:
        suffix = secrets.token_hex(3)
        base = "-".join(item for item in [normalized_user_id, normalized_platform or "device", suffix] if item)
        normalized_device_id = normalize_device_id(base)
    label = (display_name or normalized_device_id).strip()
    if not label:
        raise ValueError("device display name is required")
    token = generate_device_token()
    salt, token_hash = hash_device_token(token)
    timestamp = now()
    with connect(db_path) as conn:
        user = row(conn, "SELECT id, enabled FROM users WHERE id = ?", (normalized_user_id,))
        if not user:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        conn.execute(
            """
            INSERT INTO agent_devices (
              id, user_id, display_name, platform, token_salt, token_hash,
              enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              user_id = excluded.user_id,
              display_name = excluded.display_name,
              platform = excluded.platform,
              token_salt = excluded.token_salt,
              token_hash = excluded.token_hash,
              enabled = excluded.enabled,
              updated_at = excluded.updated_at
            """,
            (
                normalized_device_id,
                normalized_user_id,
                label,
                normalized_platform,
                salt,
                token_hash,
                int(bool(enabled)),
                timestamp,
                timestamp,
            ),
        )
    return {
        "id": normalized_device_id,
        "user_id": normalized_user_id,
        "display_name": label,
        "platform": normalized_platform,
        "enabled": bool(enabled),
        "token": token,
    }


def create_agent_enrollment_code(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    device_id: str | None = None,
    display_name: str | None = None,
    platform: str | None = "android",
    ttl_hours: int = 24,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = user_id.strip()
    normalized_platform = normalize_platform(platform) or "android"
    normalized_device_id = normalize_device_id(device_id) if device_id else None
    label = (display_name or normalized_device_id or f"{normalized_user_id}-{normalized_platform}").strip()
    ttl_hours = max(1, min(int(ttl_hours), 24 * 30))
    code = generate_enrollment_code()
    salt, code_hash = hash_device_token(code)
    timestamp = now()
    expires_at = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=ttl_hours)
    ).isoformat()
    code_id = "enroll_" + secrets.token_urlsafe(18)
    with connect(db_path) as conn:
        user = row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,))
        if not user:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        conn.execute(
            """
            INSERT INTO agent_enrollment_codes (
              id, user_id, desired_device_id, display_name, platform,
              code_salt, code_hash, enabled, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code_id,
                normalized_user_id,
                normalized_device_id,
                label,
                normalized_platform,
                salt,
                code_hash,
                int(bool(enabled)),
                expires_at,
                timestamp,
                timestamp,
            ),
        )
    return {
        "ok": True,
        "id": code_id,
        "user_id": normalized_user_id,
        "desired_device_id": normalized_device_id,
        "display_name": label,
        "platform": normalized_platform,
        "enabled": bool(enabled),
        "expires_at": expires_at,
        "code": code,
    }


def list_agent_enrollment_codes(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT c.id, c.user_id, u.display_name AS user_display_name,
                   c.desired_device_id, c.display_name, c.platform, c.enabled,
                   c.expires_at, c.used_at, c.used_device_id, c.created_at, c.updated_at
            FROM agent_enrollment_codes c
            JOIN users u ON u.id = c.user_id
            ORDER BY c.created_at DESC
            """,
        )


def revoke_agent_enrollment_code(db_path: Path, inventory_path: Path, *, code_id: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    timestamp = now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE agent_enrollment_codes SET enabled = 0, updated_at = ? WHERE id = ?",
            (timestamp, code_id.strip()),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown enrollment code: {code_id}")
    return {"ok": True, "id": code_id.strip()}


def consume_agent_enrollment_code(
    db_path: Path,
    inventory_path: Path,
    *,
    code: str,
    device_id: str | None = None,
    display_name: str | None = None,
    platform: str | None = "android",
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_code = normalize_enrollment_code(code)
    if not normalized_code:
        raise ValueError("Enrollment code is required")
    timestamp = now()
    current_dt = datetime.now(timezone.utc)
    normalized_platform = normalize_platform(platform) or "android"
    with connect(db_path) as conn:
        candidates = rows(
            conn,
            """
            SELECT c.*, u.enabled AS user_enabled
            FROM agent_enrollment_codes c
            JOIN users u ON u.id = c.user_id
            WHERE c.enabled = 1 AND c.used_at IS NULL
            ORDER BY c.created_at DESC
            """,
        )
    matched: dict[str, Any] | None = None
    for candidate in candidates:
        if not bool(candidate.get("user_enabled")):
            continue
        expires_at = parse_iso_datetime(candidate.get("expires_at"))
        if expires_at is None or expires_at < current_dt:
            continue
        if verify_device_token(normalized_code, candidate.get("code_salt"), candidate.get("code_hash")):
            matched = candidate
            break
    if matched is None:
        raise PermissionError("Invalid or expired enrollment code")

    requested_device_id = device_id or matched.get("desired_device_id") or ""
    if not requested_device_id:
        suffix = secrets.token_hex(3)
        requested_device_id = f"{matched['user_id']}-{normalized_platform}-{suffix}"
    label = display_name or matched.get("display_name") or requested_device_id
    device = create_agent_device(
        db_path,
        inventory_path,
        user_id=matched["user_id"],
        device_id=requested_device_id,
        display_name=label,
        platform=normalized_platform,
        enabled=True,
    )
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE agent_enrollment_codes
            SET used_at = ?, used_device_id = ?, enabled = 0, updated_at = ?
            WHERE id = ? AND used_at IS NULL
            """,
            (timestamp, device["id"], timestamp, matched["id"]),
        )
    return {
        "ok": True,
        "user_id": device["user_id"],
        "device_id": device["id"],
        "display_name": device["display_name"],
        "platform": device["platform"],
        "token": device["token"],
    }


def list_agent_devices(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT d.id, d.user_id, u.display_name AS user_display_name,
                   d.display_name, d.platform, d.enabled, d.last_seen_at,
                   d.last_ip, d.last_user_agent, d.created_at, d.updated_at,
                   s.reported_at AS status_reported_at
            FROM agent_devices d
            JOIN users u ON u.id = d.user_id
            LEFT JOIN agent_status s ON s.device_id = d.id
            ORDER BY d.user_id, d.id
            """,
        )


def revoke_agent_device(db_path: Path, inventory_path: Path, *, device_id: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_device_id = normalize_device_id(device_id)
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE agent_devices SET enabled = 0, updated_at = ? WHERE id = ?",
            (now(), normalized_device_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown device: {normalized_device_id}")
    return {"ok": True, "device_id": normalized_device_id, "enabled": False}


def set_agent_device_enabled(
    db_path: Path,
    inventory_path: Path,
    *,
    device_id: str,
    enabled: bool,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_device_id = normalize_device_id(device_id)
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE agent_devices SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(bool(enabled)), now(), normalized_device_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown device: {normalized_device_id}")
    return {"ok": True, "device_id": normalized_device_id, "enabled": bool(enabled)}


def delete_agent_device(db_path: Path, inventory_path: Path, *, device_id: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_device_id = normalize_device_id(device_id)
    timestamp = now()
    with connect(db_path) as conn:
        existing = row(conn, "SELECT id FROM agent_devices WHERE id = ?", (normalized_device_id,))
        if not existing:
            raise ValueError(f"Unknown device: {normalized_device_id}")
        conn.execute(
            "UPDATE agent_enrollment_codes SET used_device_id = NULL, updated_at = ? WHERE used_device_id = ?",
            (timestamp, normalized_device_id),
        )
        conn.execute(
            """
            UPDATE agent_probe_jobs
            SET assigned_device_id = CASE WHEN assigned_device_id = ? THEN '' ELSE assigned_device_id END,
                claimed_by_device_id = CASE WHEN claimed_by_device_id = ? THEN '' ELSE claimed_by_device_id END,
                updated_at = ?
            WHERE assigned_device_id = ? OR claimed_by_device_id = ?
            """,
            (normalized_device_id, normalized_device_id, timestamp, normalized_device_id, normalized_device_id),
        )
        conn.execute("DELETE FROM agent_status WHERE device_id = ?", (normalized_device_id,))
        conn.execute("DELETE FROM agent_devices WHERE id = ?", (normalized_device_id,))
    return {"ok": True, "device_id": normalized_device_id, "deleted": True}


def agent_status_rows(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        entries = rows(
            conn,
            """
            SELECT d.id AS device_id, d.user_id, d.display_name, d.platform,
                   d.enabled, d.last_seen_at, d.last_ip, s.reported_at, s.status_json
            FROM agent_devices d
            LEFT JOIN agent_status s ON s.device_id = d.id
            ORDER BY d.user_id, d.id
            """,
        )
    for entry in entries:
        try:
            entry["status"] = json.loads(entry.pop("status_json") or "{}")
        except json.JSONDecodeError:
            entry["status"] = {}
    return entries


def save_agent_diagnostic_report(
    db_path: Path,
    inventory_path: Path,
    *,
    device: dict[str, Any],
    summary: str,
    report_text: str,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    timestamp = now()
    report_id = f"diag-{int(time.time())}-{secrets.token_hex(4)}"
    normalized_summary = str(summary or "").strip()[:500]
    normalized_report = str(report_text or "")
    max_report_chars = 200_000
    if len(normalized_report) > max_report_chars:
        normalized_report = normalized_report[-max_report_chars:]
        normalized_summary = (normalized_summary + " [truncated]").strip()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO agent_diagnostics (
              id, device_id, user_id, platform, summary, report_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                device["id"],
                device["user_id"],
                str(device.get("platform") or ""),
                normalized_summary,
                normalized_report,
                timestamp,
            ),
        )
    return {
        "ok": True,
        "id": report_id,
        "device_id": device["id"],
        "created_at": timestamp,
        "bytes": len(normalized_report.encode("utf-8")),
    }


def agent_diagnostic_rows(db_path: Path, inventory_path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT id, device_id, user_id, platform, summary, report_text, created_at
            FROM agent_diagnostics
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        )


def count_value(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> int:
    item = row(conn, sql, params)
    if not item:
        return 0
    return int(next(iter(item.values())) or 0)


def grouped_counts(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in rows(conn, sql, params):
        key = str(item.get("key") or "")
        result[key] = int(item.get("count") or 0)
    return result


def build_system_status(
    db_path: Path,
    inventory_path: Path,
    *,
    include_external: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    reference = datetime.now(timezone.utc)
    with connect(db_path) as conn:
        agent_recent_seconds = 180
        agent_status_items = rows(
            conn,
            """
            SELECT d.id, d.user_id, d.platform, d.enabled, d.last_seen_at,
                   s.reported_at, s.status_json
            FROM agent_devices d
            LEFT JOIN agent_status s ON s.device_id = d.id
            ORDER BY d.user_id, d.id
            """,
        )
        agents: list[dict[str, Any]] = []
        for item in agent_status_items:
            reported_age = timestamp_age_seconds(item.get("reported_at"), reference=reference)
            last_seen_age = timestamp_age_seconds(item.get("last_seen_at"), reference=reference)
            health_ok = None
            try:
                status_json = json.loads(item.get("status_json") or "{}")
                if isinstance(status_json, dict) and isinstance(status_json.get("health"), dict):
                    health_ok = status_json["health"].get("ok")
            except json.JSONDecodeError:
                pass
            online = bool(item.get("enabled")) and reported_age is not None and reported_age <= agent_recent_seconds
            agents.append(
                {
                    "id": item["id"],
                    "user_id": item["user_id"],
                    "platform": item.get("platform") or "",
                    "enabled": bool(item.get("enabled")),
                    "online": online,
                    "health_ok": health_ok,
                    "last_seen_at": item.get("last_seen_at"),
                    "last_seen_age_seconds": last_seen_age,
                    "reported_at": item.get("reported_at"),
                    "reported_age_seconds": reported_age,
                }
            )
        transports = transport_config_summaries(conn)
        oldest_transport_age = None
        newest_transport_age = None
        transport_ages = [
            age
            for age in (timestamp_age_seconds(item.get("updated_at"), reference=reference) for item in transports)
            if age is not None
        ]
        if transport_ages:
            oldest_transport_age = max(transport_ages)
            newest_transport_age = min(transport_ages)
        provider_rows = rows(
            conn,
            """
            SELECT COALESCE(NULLIF(s.provider, ''), 'unknown') AS key,
                   COUNT(*) AS count,
                   SUM(CASE WHEN t.enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                   SUM(CASE WHEN t.enabled = 1 AND s.enabled = 1 THEN 1 ELSE 0 END) AS active,
                   MIN(t.updated_at) AS oldest_updated_at,
                   MAX(t.updated_at) AS newest_updated_at
            FROM transport_configs t
            JOIN servers s ON s.id = t.server_id
            GROUP BY COALESCE(NULLIF(s.provider, ''), 'unknown')
            ORDER BY key
            """,
        )
        providers: dict[str, dict[str, Any]] = {}
        for item in provider_rows:
            providers[str(item["key"])] = {
                "total": int(item.get("count") or 0),
                "enabled": int(item.get("enabled") or 0),
                "active": int(item.get("active") or 0),
                "oldest_updated_at": item.get("oldest_updated_at"),
                "oldest_age_seconds": timestamp_age_seconds(item.get("oldest_updated_at"), reference=reference),
                "newest_updated_at": item.get("newest_updated_at"),
                "newest_age_seconds": timestamp_age_seconds(item.get("newest_updated_at"), reference=reference),
            }
        stale_transports: list[dict[str, Any]] = []
        stale_by_provider: dict[str, int] = {}
        for item in transports:
            age = timestamp_age_seconds(item.get("updated_at"), reference=reference)
            if (
                not item.get("active")
                or not provider_transport_required(item)
                or age is None
                or age <= TRANSPORT_STALE_WARN_SECONDS
            ):
                continue
            provider = item.get("provider") or "unknown"
            stale_by_provider[provider] = stale_by_provider.get(provider, 0) + 1
            stale_transports.append(
                {
                    "server_id": item.get("server_id"),
                    "label": item.get("label"),
                    "provider": provider,
                    "updated_at": item.get("updated_at"),
                    "age_seconds": age,
                }
            )
        fallback = (
            cudy_fallback_state_status()
            if include_external
            else {
                "url": CUDY_FALLBACK_STATE_URL,
                "reachable": None,
                "ok": None,
                "skipped": True,
            }
        )
        warnings: list[str] = []
        advisories: list[str] = []
        if include_external and CUDY_FALLBACK_STATUS_WARN and not fallback.get("ok"):
            warnings.append("Cudy fallback state is stale or unreachable from this process")
        pending_probe_jobs = count_value(conn, "SELECT COUNT(*) AS count FROM agent_probe_jobs WHERE status = 'pending'")
        failed_probe_jobs = count_value(conn, "SELECT COUNT(*) AS count FROM agent_probe_jobs WHERE status = 'failed'")
        domain_discovery_by_status = grouped_counts(conn, "SELECT status AS key, COUNT(*) AS count FROM domain_discovery_queue GROUP BY status")
        pending_domain_discovery = int(domain_discovery_by_status.get("pending") or 0)
        total_domain_discovery = sum(int(value or 0) for value in domain_discovery_by_status.values())
        latest_domain_discovery = row(conn, "SELECT MAX(last_seen_at) AS value FROM domain_discovery_queue")
        failed_probe_cutoff_epoch = reference.replace(microsecond=0).timestamp() - PROBE_FAILED_WARN_SECONDS
        failed_probe_cutoff = datetime.fromtimestamp(failed_probe_cutoff_epoch, timezone.utc).replace(microsecond=0).isoformat()
        failed_recent_rows = rows(
            conn,
            """
            SELECT url, result_json
            FROM agent_probe_jobs
            WHERE status = 'failed'
              AND COALESCE(finished_at, updated_at, created_at) >= ?
            """,
            (failed_probe_cutoff,),
        )
        failed_recent_unresolvable = sum(
            1
            for item in failed_recent_rows
            if not (item.get("url") or "").strip()
            and probe_result_all_unresolvable(item.get("result_json"))
        )
        failed_recent_probe_jobs = len(failed_recent_rows) - failed_recent_unresolvable
        oldest_pending_probe = row(conn, "SELECT MIN(created_at) AS value FROM agent_probe_jobs WHERE status = 'pending'")
        latest_probe_created = row(conn, "SELECT MAX(created_at) AS value FROM agent_probe_jobs")
        latest_probe_updated = row(conn, "SELECT MAX(updated_at) AS value FROM agent_probe_jobs")
        latest_probe_finished = row(conn, "SELECT MAX(finished_at) AS value FROM agent_probe_jobs WHERE finished_at IS NOT NULL")
        offline_enabled_agents = sum(1 for item in agents if item["enabled"] and not item["online"])
        if offline_enabled_agents:
            advisories.append(f"{offline_enabled_agents} enabled agent(s) are offline or stale")
        if pending_domain_discovery:
            advisories.append(f"{pending_domain_discovery} discovered domain(s) are pending admin review")
        if failed_recent_probe_jobs:
            warnings.append(f"{failed_recent_probe_jobs} probe job(s) failed within {PROBE_FAILED_WARN_SECONDS}s")
        if stale_transports:
            details = ", ".join(f"{key}={value}" for key, value in sorted(stale_by_provider.items()))
            warnings.append(f"{len(stale_transports)} enabled transport config(s) are stale over {TRANSPORT_STALE_WARN_SECONDS}s ({details})")
        local_backup = latest_file_status(CONTROL_BACKUP_DIR, "cudy-control-*.tgz", reference=reference)
        backup_task_log = file_status(CONTROL_BACKUP_DIR / "backup-task.log", reference=reference)
        fallback_sync_log = file_status(CONTROL_BACKUP_DIR / "cudy-fallback-sync.log", reference=reference)
        if CONTROL_BACKUP_STATUS_WARN and not local_backup.get("exists"):
            warnings.append("No local control-server backup archive was found")
        if CONTROL_BACKUP_STATUS_WARN and local_backup.get("age_seconds") is not None and local_backup["age_seconds"] > CONTROL_BACKUP_MAX_AGE_SECONDS:
            warnings.append("Latest local control-server backup is stale")
        if LOCAL_FALLBACK_SYNC_STATUS_WARN and not fallback_sync_log.get("exists"):
            warnings.append("No local Cudy fallback sync log was found")
        if (
            LOCAL_FALLBACK_SYNC_STATUS_WARN
            and fallback_sync_log.get("age_seconds") is not None
            and fallback_sync_log["age_seconds"] > CUDY_FALLBACK_MAX_AGE_SECONDS
        ):
            warnings.append("Latest local Cudy fallback sync run is stale")
        return {
            "ok": not warnings,
            "generated_at": now(),
            "service": {
                "started_at": APP_STARTED_AT,
                "uptime_seconds": timestamp_age_seconds(APP_STARTED_AT, reference=reference),
                "pid": os.getpid(),
            },
            "database": {
                "path": str(db_path),
                "users": count_value(conn, "SELECT COUNT(*) AS count FROM users"),
                "enabled_users": count_value(conn, "SELECT COUNT(*) AS count FROM users WHERE enabled = 1"),
                "servers": count_value(conn, "SELECT COUNT(*) AS count FROM servers"),
                "enabled_servers": count_value(conn, "SELECT COUNT(*) AS count FROM servers WHERE enabled = 1"),
                "global_domain_routes": count_value(conn, "SELECT COUNT(*) AS count FROM global_domain_routes WHERE enabled = 1"),
                "user_domain_routes": count_value(conn, "SELECT COUNT(*) AS count FROM user_domain_routes WHERE enabled = 1"),
                "global_ip_routes": count_value(conn, "SELECT COUNT(*) AS count FROM global_ip_routes WHERE enabled = 1"),
                "auto_cache": count_value(conn, "SELECT COUNT(*) AS count FROM domain_auto_cache"),
            },
            "agents": {
                "total": len(agents),
                "enabled": sum(1 for item in agents if item["enabled"]),
                "online": sum(1 for item in agents if item["online"]),
                "offline_enabled": offline_enabled_agents,
                "recent_seconds": agent_recent_seconds,
                "items": agents[:50],
            },
            "probe_jobs": {
                "by_status": grouped_counts(conn, "SELECT status AS key, COUNT(*) AS count FROM agent_probe_jobs GROUP BY status"),
                "pending": pending_probe_jobs,
                "failed": failed_probe_jobs,
                "failed_recent": failed_recent_probe_jobs,
                "failed_recent_unresolvable": failed_recent_unresolvable,
                "failed_warn_seconds": PROBE_FAILED_WARN_SECONDS,
                "oldest_pending_created_at": (oldest_pending_probe or {}).get("value"),
                "oldest_pending_age_seconds": timestamp_age_seconds((oldest_pending_probe or {}).get("value"), reference=reference),
                "latest_created_at": (latest_probe_created or {}).get("value"),
                "latest_created_age_seconds": timestamp_age_seconds((latest_probe_created or {}).get("value"), reference=reference),
                "latest_updated_at": (latest_probe_updated or {}).get("value"),
                "latest_updated_age_seconds": timestamp_age_seconds((latest_probe_updated or {}).get("value"), reference=reference),
                "latest_finished_at": (latest_probe_finished or {}).get("value"),
                "latest_finished_age_seconds": timestamp_age_seconds((latest_probe_finished or {}).get("value"), reference=reference),
            },
            "domain_discovery": {
                "by_status": domain_discovery_by_status,
                "total": total_domain_discovery,
                "pending": pending_domain_discovery,
                "latest_seen_at": (latest_domain_discovery or {}).get("value"),
                "latest_seen_age_seconds": timestamp_age_seconds((latest_domain_discovery or {}).get("value"), reference=reference),
            },
            "transports": {
                "total": len(transports),
                "enabled": sum(1 for item in transports if item["enabled"]),
                "active": sum(1 for item in transports if item["active"]),
                "by_provider": {key: value["total"] for key, value in providers.items()},
                "providers": providers,
                "newest_age_seconds": newest_transport_age,
                "oldest_age_seconds": oldest_transport_age,
                "stale_warn_seconds": TRANSPORT_STALE_WARN_SECONDS,
                "stale_enabled": stale_transports[:50],
                "stale_enabled_count": len(stale_transports),
                "stale_by_provider": stale_by_provider,
            },
            "workers": worker_status_snapshot(db_path, reference=reference),
            "operations": {
                "local_backup": {
                    "max_age_seconds": CONTROL_BACKUP_MAX_AGE_SECONDS,
                    "status_warn_enabled": CONTROL_BACKUP_STATUS_WARN,
                    "latest_archive": local_backup,
                    "task_log": backup_task_log,
                },
                "local_cudy_fallback_sync": {
                    "max_age_seconds": CUDY_FALLBACK_MAX_AGE_SECONDS,
                    "status_warn_enabled": LOCAL_FALLBACK_SYNC_STATUS_WARN,
                    "task_log": fallback_sync_log,
                },
            },
            "control": {
                "endpoints": control_endpoints_manifest(),
                "cudy_fallback_state": fallback,
            },
            "warnings": warnings,
            "advisories": advisories,
        }


def build_readiness_status(db_path: Path, inventory_path: Path) -> dict[str, Any]:
    # Readiness must only describe the local control process. A slow or offline
    # Cudy fallback is operational status, not a reason to block this endpoint.
    status = build_system_status(db_path, inventory_path, include_external=False)
    probe_jobs = status.get("probe_jobs") or {}
    transports = status.get("transports") or {}
    failed_recent = int(probe_jobs.get("failed_recent") or 0)
    stale_transports = int(transports.get("stale_enabled_count") or 0)
    checks = [
        {
            "name": "control_server",
            "ok": True,
            "summary": "ready",
        },
        {
            "name": "agents",
            "ok": True,
            "summary": (
                f"{(status.get('agents') or {}).get('online') or 0}/"
                f"{(status.get('agents') or {}).get('enabled') or 0} online"
            ),
        },
        {
            "name": "probe_jobs",
            "ok": True,
            "state": "warn" if failed_recent else "ok",
            "summary": (
                f"{probe_jobs.get('pending') or 0} pending, "
                f"{failed_recent} recent failed"
            ),
        },
        {
            "name": "domain_discovery",
            "ok": True,
            "summary": (
                f"{(status.get('domain_discovery') or {}).get('pending') or 0} pending, "
                f"{(status.get('domain_discovery') or {}).get('total') or 0} total"
            ),
        },
        {
            "name": "transports",
            "ok": stale_transports == 0,
            "summary": (
                f"{transports.get('active') or 0}/"
                f"{transports.get('total') or 0} active"
            ),
        },
    ]
    return {
        "ok": all(bool(item.get("ok")) for item in checks),
        "generated_at": status.get("generated_at"),
        "service": status.get("service") or {},
        "checks": checks,
        "warnings": status.get("warnings") or [],
        "advisories": status.get("advisories") or [],
    }


def recent_probe_transport_ids(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    warm_seconds: int = ANDROID_PROBE_TRANSPORT_WARM_SECONDS,
    max_ids: int = ANDROID_PROBE_TRANSPORT_WARM_LIMIT,
) -> list[str]:
    if not device_id or warm_seconds <= 0 or max_ids <= 0:
        return []
    cutoff = (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=warm_seconds)).isoformat()
    recent_jobs = rows(
        conn,
        """
        SELECT candidate_server_ids
        FROM agent_probe_jobs
        WHERE claimed_by_device_id = ?
          AND status IN ('running', 'done', 'failed')
          AND COALESCE(finished_at, updated_at, started_at, created_at) >= ?
        ORDER BY COALESCE(finished_at, updated_at, started_at, created_at) DESC
        LIMIT 100
        """,
        (device_id, cutoff),
    )
    result: list[str] = []
    seen: set[str] = set()
    for job in recent_jobs:
        try:
            candidates = json.loads(job.get("candidate_server_ids") or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(candidates, list):
            continue
        for value in candidates:
            server_id = str(value or "").strip()
            if not server_id or server_id in seen:
                continue
            seen.add(server_id)
            result.append(server_id)
            if len(result) >= max_ids:
                return result
    return result


def build_agent_config(conn: sqlite3.Connection, *, user_id: str, device: dict[str, Any]) -> dict[str, Any]:
    servers = server_map(conn)
    cached_auto = auto_cache_map(conn)
    user = row(
        conn,
        """
        SELECT id, display_name, role, default_server_id, client_ip, enabled
        FROM users
        WHERE id = ? AND enabled = 1
        """,
        (user_id,),
    )
    if not user:
        raise PermissionError("Agent user is disabled or missing")
    effective: dict[str, dict[str, Any]] = {}

    def add_service_group_routes(*, scope: str, overwrite: bool) -> None:
        for service in effective_critical_services(conn, user_id=user_id):
            if service.get("scope") != scope or not service.get("routing_enabled"):
                continue
            cache_key = service_auto_cache_key(str(service.get("user_id") or ""), str(service["service_key"]))
            policy = service_group_policy(conn, service)
            for domain in critical_service_target_hosts(service):
                route = {
                    "domain": domain,
                    "server_id": "auto",
                    "source": f"{scope}_service_group",
                    "service_key": service["service_key"],
                    "service_label": service.get("label") or service["service_key"],
                    "auto_cache_key": cache_key,
                    "auto_candidate_policy": policy,
                    "updated_at": service.get("updated_at") or "",
                }
                if overwrite:
                    effective[domain] = route
                else:
                    effective.setdefault(domain, route)

    add_service_group_routes(scope="global", overwrite=False)
    for route in rows(
        conn,
        """
        SELECT domain, server_id, updated_at
        FROM global_domain_routes
        WHERE enabled = 1
        ORDER BY domain
        """,
    ):
        effective[route["domain"]] = {**route, "source": "global"}
    add_service_group_routes(scope="user", overwrite=True)
    for route in rows(
        conn,
        """
        SELECT domain, server_id, updated_at
        FROM user_domain_routes
        WHERE user_id = ? AND enabled = 1
        ORDER BY domain
        """,
        (user_id,),
    ):
        effective[route["domain"]] = {**route, "source": "user"}

    domain_routes: list[dict[str, Any]] = []
    warnings: list[str] = []
    referenced_server_ids: set[str] = set()
    for route in sorted(effective.values(), key=lambda item: item["domain"]):
        requested_server_id = route["server_id"]
        route_warnings: list[str] = []
        cache_key = str(route.get("auto_cache_key") or route["domain"])
        auto_policy = route.get("auto_candidate_policy")
        if requested_server_id == "auto" and auto_policy is None:
            auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=route["domain"])
        resolved_server_id, cached = resolve_route_server(
            domain=cache_key,
            requested_server_id=requested_server_id,
            servers=servers,
            auto_cache=cached_auto,
            auto_policy=auto_policy,
            context=f"{user_id}/{route['domain']}",
            warnings=route_warnings,
        )
        warnings.extend(route_warnings)
        server_id = resolved_server_id or ("direct" if requested_server_id == "auto" else requested_server_id)
        referenced_server_ids.add(server_id)
        domain_routes.append(
            {
                "domain": route["domain"],
                "source": route["source"],
                "requested_server_id": requested_server_id,
                "server_id": server_id,
                "resolved_server_id": resolved_server_id,
                "server": compact_server(servers.get(server_id)),
                "auto_cache_key": cache_key if requested_server_id == "auto" else "",
                "auto_cache": cached,
                "auto_candidate_policy": auto_policy,
                "service_key": route.get("service_key") or "",
                "service_label": route.get("service_label") or "",
                "updated_at": route["updated_at"],
            }
        )

    ip_routes = rows(
        conn,
        """
        SELECT target_cidr, server_id, enabled, updated_at, 'global' AS source
        FROM global_ip_routes
        WHERE enabled = 1
        UNION ALL
        SELECT target_cidr, server_id, enabled, updated_at, 'user' AS source
        FROM user_ip_routes
        WHERE user_id = ? AND enabled = 1
        ORDER BY target_cidr, source
        """,
        (user_id,),
    )
    effective_ip_routes: dict[str, dict[str, Any]] = {}
    for route in ip_routes:
        effective_ip_routes[route["target_cidr"]] = route
    ip_routes = sorted(effective_ip_routes.values(), key=lambda item: item["target_cidr"])
    resolved_ip_routes: list[dict[str, Any]] = []
    cleanup_ip_routes: list[dict[str, Any]] = []
    for route in ip_routes:
        requested_server_id = str(route["server_id"])
        resolved_server_id = None
        cached = None
        auto_policy = None
        cache_key = ""
        server_id = requested_server_id
        if requested_server_id == "auto":
            cache_key = auto_cache_key_for_ip_route(route["target_cidr"])
            route_warnings: list[str] = []
            auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=cache_key)
            resolved_server_id, cached = resolve_route_server(
                domain=cache_key,
                requested_server_id=requested_server_id,
                servers=servers,
                auto_cache=cached_auto,
                auto_policy=auto_policy,
                context=f"{user_id}/{route['target_cidr']}",
                warnings=route_warnings,
            )
            warnings.extend(route_warnings)
            if not resolved_server_id:
                cleanup_ip_routes.append(
                    {
                        "target_cidr": route["target_cidr"],
                        "source": route["source"],
                        "requested_server_id": requested_server_id,
                        "server_id": "",
                        "resolved_server_id": None,
                        "auto_cache_key": cache_key,
                        "auto_cache": cached,
                        "auto_candidate_policy": auto_policy,
                        "updated_at": route["updated_at"],
                    }
                )
                continue
            server_id = resolved_server_id
        route["requested_server_id"] = requested_server_id
        route["server_id"] = server_id
        route["resolved_server_id"] = resolved_server_id
        route["auto_cache_key"] = cache_key
        route["auto_cache"] = cached
        route["auto_candidate_policy"] = auto_policy
        route["server"] = compact_server(servers.get(server_id))
        referenced_server_ids.add(server_id)
        resolved_ip_routes.append(route)
    ip_routes = resolved_ip_routes

    for job in rows(
        conn,
        """
        SELECT candidate_server_ids
        FROM agent_probe_jobs
        WHERE status = 'pending'
          AND (assigned_device_id = '' OR assigned_device_id = ?)
          AND (user_id = '' OR user_id = ?)
        ORDER BY priority ASC, created_at ASC
        LIMIT 10
        """,
        (device["id"], user_id),
    ):
        try:
            candidates = json.loads(job.get("candidate_server_ids") or "[]")
        except json.JSONDecodeError:
            candidates = []
        for server_id in candidates:
            if server_id in servers:
                referenced_server_ids.add(server_id)

    if str(device.get("platform") or "").strip().lower() == "android":
        android_transport_types = {
            item["server_id"]: item["transport_type"]
            for item in transport_config_rows(conn, enabled_only=True)
        }
        for server_id in recent_probe_transport_ids(conn, device_id=str(device.get("id") or "")):
            if (
                server_id in servers
                and android_transport_types.get(server_id) in ANDROID_SUPPORTED_TRANSPORT_TYPES
            ):
                referenced_server_ids.add(server_id)

    transport_plan = build_transport_plan(conn, server_ids=referenced_server_ids, warnings=warnings)

    return {
        "schema_version": 1,
        "generated_at": now(),
        "device": {
            "id": device["id"],
            "display_name": device["display_name"],
            "platform": device.get("platform") or "",
        },
        "user": user,
        "control": {
            "poll_seconds": 60,
            "status_seconds": 60,
            "endpoints": control_endpoints_manifest(),
            "reserved_targets": [
                {"id": "direct", "label": "Direct internet", "kind": "local"},
                {"id": "auto", "label": "Auto", "kind": "virtual"},
            ],
        },
        "servers": user_servers(conn),
        "default_server_id": user["default_server_id"],
        "default_server": compact_server(servers.get(user["default_server_id"])),
        "domain_routes": domain_routes,
        "ip_routes": ip_routes,
        "cleanup_ip_routes": cleanup_ip_routes,
        "transport_plan": transport_plan,
        "auto_candidates": auto_candidate_policy_rows(conn),
        "critical_services": effective_critical_services(conn, user_id=user_id),
        "warnings": warnings,
    }


def list_user_domain_routes(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT r.user_id, u.client_ip, r.domain, r.server_id, r.enabled, r.updated_at
            FROM user_domain_routes r
            JOIN users u ON u.id = r.user_id
            ORDER BY r.user_id, r.server_id, r.domain
            """,
        )


def save_user_domain_route(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    domain: str,
    server_id: str,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = user_id.strip()
    normalized_domain = normalize_domain(domain)
    timestamp = now()
    with connect(db_path) as conn:
        if row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,)) is None:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        validate_server_id(conn, server_id, require_user_visible=False)
        conn.execute(
            """
            INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, domain)
            DO UPDATE SET server_id = excluded.server_id, enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (normalized_user_id, normalized_domain, server_id, int(enabled), timestamp, timestamp),
        )
    return {"ok": True, "user_id": normalized_user_id, "domain": normalized_domain, "server_id": server_id}


def delete_user_domain_route(db_path: Path, inventory_path: Path, *, user_id: str, domain: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = user_id.strip()
    normalized_domain = normalize_domain(domain)
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM user_domain_routes WHERE user_id = ? AND domain = ?",
            (normalized_user_id, normalized_domain),
        )
    return {"ok": True, "user_id": normalized_user_id, "domain": normalized_domain}


def list_global_domain_routes(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT domain, server_id, enabled, updated_at
            FROM global_domain_routes
            ORDER BY server_id, domain
            """,
        )


def save_global_domain_route(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    server_id: str,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_domain = normalize_domain(domain)
    timestamp = now()
    with connect(db_path) as conn:
        if server_id != "auto":
            validate_server_id(conn, server_id, require_user_visible=False)
        conn.execute(
            """
            INSERT INTO global_domain_routes (domain, server_id, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(domain)
            DO UPDATE SET server_id = excluded.server_id,
                          enabled = excluded.enabled,
                          updated_at = excluded.updated_at
            """,
            (normalized_domain, server_id, int(enabled), timestamp, timestamp),
        )
    return {"ok": True, "domain": normalized_domain, "server_id": server_id}


def delete_global_domain_route(db_path: Path, inventory_path: Path, *, domain: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_domain = normalize_domain(domain)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM global_domain_routes WHERE domain = ?", (normalized_domain,))
    return {"ok": True, "domain": normalized_domain}


def iter_domain_override_file(path: Path) -> Iterable[str]:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        yield normalize_domain(line)


def import_global_domain_routes(
    db_path: Path,
    inventory_path: Path,
    *,
    input_files: list[Path],
    server_id: str,
    enabled: bool = True,
) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    for input_file in input_files:
        for domain in iter_domain_override_file(input_file):
            imported.append(
                save_global_domain_route(
                    db_path,
                    inventory_path,
                    domain=domain,
                    server_id=server_id,
                    enabled=enabled,
                )
            )
    return {"ok": True, "server_id": server_id, "imported": imported, "count": len(imported)}


def list_user_ip_routes(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT r.user_id, u.client_ip, r.target_cidr, r.server_id, r.enabled, r.updated_at
            FROM user_ip_routes r
            JOIN users u ON u.id = r.user_id
            ORDER BY r.user_id, r.server_id, r.target_cidr
            """,
        )


def list_global_ip_routes(db_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    init_db(db_path, inventory_path)
    with connect(db_path) as conn:
        return rows(
            conn,
            """
            SELECT target_cidr, server_id, enabled, source, note, updated_at
            FROM global_ip_routes
            ORDER BY server_id, target_cidr
            """,
        )


def save_global_ip_route(
    db_path: Path,
    inventory_path: Path,
    *,
    target_cidr: str,
    server_id: str,
    enabled: bool = True,
    source: str = "",
    note: str = "",
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_cidr = normalize_ipv4_cidr(target_cidr)
    timestamp = now()
    with connect(db_path) as conn:
        if server_id != "auto":
            validate_server_id(conn, server_id, require_user_visible=False)
        conn.execute(
            """
            INSERT INTO global_ip_routes (target_cidr, server_id, enabled, created_at, updated_at, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_cidr)
            DO UPDATE SET server_id = excluded.server_id,
                          enabled = excluded.enabled,
                          updated_at = excluded.updated_at,
                          source = excluded.source,
                          note = excluded.note
            """,
            (normalized_cidr, server_id, int(enabled), timestamp, timestamp, source, note),
        )
    return {"ok": True, "target_cidr": normalized_cidr, "server_id": server_id}


def delete_global_ip_route(db_path: Path, inventory_path: Path, *, target_cidr: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_cidr = normalize_ipv4_cidr(target_cidr)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM global_ip_routes WHERE target_cidr = ?", (normalized_cidr,))
    return {"ok": True, "target_cidr": normalized_cidr}


def iter_ip_override_file(path: Path) -> Iterable[str]:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        yield normalize_ipv4_cidr(line)


def import_global_ip_routes(
    db_path: Path,
    inventory_path: Path,
    *,
    input_files: list[Path],
    server_id: str,
    source: str = "override-file",
    note: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    for input_file in input_files:
        for cidr in iter_ip_override_file(input_file):
            imported.append(
                save_global_ip_route(
                    db_path,
                    inventory_path,
                    target_cidr=cidr,
                    server_id=server_id,
                    enabled=enabled,
                    source=source,
                    note=note or str(input_file),
                )
            )
    return {"ok": True, "server_id": server_id, "imported": imported, "count": len(imported)}


def save_user_ip_route(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    target_cidr: str,
    server_id: str,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = user_id.strip()
    normalized_cidr = normalize_ipv4_cidr(target_cidr)
    timestamp = now()
    with connect(db_path) as conn:
        if row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,)) is None:
            raise ValueError(f"Unknown user: {normalized_user_id}")
        if server_id != "auto":
            validate_server_id(conn, server_id, require_user_visible=False)
        conn.execute(
            """
            INSERT INTO user_ip_routes (user_id, target_cidr, server_id, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, target_cidr)
            DO UPDATE SET server_id = excluded.server_id, enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (normalized_user_id, normalized_cidr, server_id, int(enabled), timestamp, timestamp),
        )
    return {"ok": True, "user_id": normalized_user_id, "target_cidr": normalized_cidr, "server_id": server_id}


def delete_user_ip_route(db_path: Path, inventory_path: Path, *, user_id: str, target_cidr: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_user_id = user_id.strip()
    normalized_cidr = normalize_ipv4_cidr(target_cidr)
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM user_ip_routes WHERE user_id = ? AND target_cidr = ?",
            (normalized_user_id, normalized_cidr),
        )
    return {"ok": True, "user_id": normalized_user_id, "target_cidr": normalized_cidr}


def build_route_plan(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        servers = server_map(conn)
        cached_auto = auto_cache_map(conn)
        global_routes = rows(
            conn,
            """
            SELECT domain, server_id, enabled, updated_at
            FROM global_domain_routes
            WHERE enabled = 1
            ORDER BY domain
            """,
        )
        users = rows(
            conn,
            """
            SELECT id, display_name, role, default_server_id, client_ip, enabled
            FROM users
            WHERE enabled = 1
              AND role = 'user'
              AND (client_ip IS NOT NULL OR password_hash IS NOT NULL)
            ORDER BY id
            """,
        )
        plan_users: list[dict[str, Any]] = []
        warnings: list[str] = []
        for user in users:
            effective: dict[str, dict[str, Any]] = {}
            user_warnings: list[str] = []
            client_ip = normalize_client_ip(user.get("client_ip")) if user.get("client_ip") else None
            if not client_ip:
                user_warnings.append("client_ip is missing; Cudy cannot apply individual source-based routing yet")
            default_server = servers.get(user["default_server_id"])
            if not default_server:
                user_warnings.append(f"default server is unknown: {user['default_server_id']}")
            for route in global_routes:
                effective[route["domain"]] = {
                    "domain": route["domain"],
                    "server_id": route["server_id"],
                    "source": "global",
                }
            user_routes = rows(
                conn,
                """
                SELECT domain, server_id, enabled, updated_at
                FROM user_domain_routes
                WHERE user_id = ? AND enabled = 1
                ORDER BY domain
                """,
                (user["id"],),
            )
            for route in user_routes:
                effective[route["domain"]] = {
                    "domain": route["domain"],
                    "server_id": route["server_id"],
                    "source": "user",
                }
            route_items: list[dict[str, Any]] = []
            for route in sorted(effective.values(), key=lambda item: item["domain"]):
                requested_server_id = route["server_id"]
                route_warnings: list[str] = []
                auto_policy = (
                    resolve_auto_candidate_policy(conn, user_id=user["id"], domain=route["domain"])
                    if requested_server_id == "auto"
                    else None
                )
                resolved_server_id, cached = resolve_route_server(
                    domain=route["domain"],
                    requested_server_id=requested_server_id,
                    servers=servers,
                    auto_cache=cached_auto,
                    auto_policy=auto_policy,
                    context=f"{user['id']}/{route['domain']}",
                    warnings=route_warnings,
                )
                user_warnings.extend(route_warnings)
                server_id = resolved_server_id or requested_server_id
                server = servers.get(server_id)
                if not server:
                    user_warnings.append(f"{route['domain']}: unknown server {server_id}")
                route_items.append(
                    {
                        **route,
                        "requested_server_id": requested_server_id,
                        "server_id": server_id,
                        "resolved_server_id": resolved_server_id,
                        "auto_cache": cached,
                        "auto_candidate_policy": auto_policy,
                        "server": compact_server(server),
                    }
                )
            for warning in user_warnings:
                warnings.append(f"{user['id']}: {warning}")
            plan_users.append(
                {
                    "id": user["id"],
                    "display_name": user["display_name"],
                    "role": user["role"],
                    "client_ip": client_ip,
                    "default_server_id": user["default_server_id"],
                    "default_server": compact_server(default_server),
                    "routes": route_items,
                    "warnings": user_warnings,
                }
            )
    return {
        "generated_at": now(),
        "summary": {
            "users": len(plan_users),
            "global_routes": len(global_routes),
            "effective_routes": sum(len(user["routes"]) for user in plan_users),
            "warnings": len(warnings),
        },
        "users": plan_users,
        "warnings": warnings,
    }


def build_combined_deploy_preview(
    db_path: Path,
    inventory_path: Path,
    *,
    pbr_output_dir: Path = DEFAULT_PBR_EXPORT_DIR,
    user_output_dir: Path = DEFAULT_USER_ROUTES_EXPORT_DIR,
    pbr_remote_dir: str = REMOTE_PBR_DIR,
    user_remote_dir: str = REMOTE_USER_ROUTES_DIR,
    ssh_host: str = DEFAULT_CUDY_HOST,
    install_scripts: bool = False,
    restart_pbr: bool = True,
    run_user_apply: bool = True,
    prune_empty: bool = False,
    allow_empty: bool = False,
) -> dict[str, Any]:
    pbr_manifest = export_pbr_overrides(db_path, inventory_path, pbr_output_dir)
    user_manifest = export_user_routes(db_path, inventory_path, user_output_dir)
    pbr_plan = build_pbr_deploy_plan(
        pbr_manifest,
        ssh_host=ssh_host,
        remote_dir=pbr_remote_dir,
        install_scripts=install_scripts,
        restart_pbr=restart_pbr,
        prune_empty=prune_empty,
    )
    user_plan = build_user_routes_deploy_plan(
        user_manifest,
        ssh_host=ssh_host,
        remote_dir=user_remote_dir,
        install_script=install_scripts,
        run_apply=run_user_apply,
    )
    should_apply_pbr = (
        pbr_plan["route_count"] > 0
        or prune_empty
        or install_scripts
        or allow_empty
    )
    should_apply_user = user_plan["route_count"] > 0 or allow_empty
    warnings: list[str] = []
    if should_apply_pbr:
        warnings.extend(pbr_plan["warnings"])
    if should_apply_user:
        warnings.extend(
            warning
            for warning in user_plan["warnings"]
            if "remote user-route apply script is not updated" not in warning
        )
    if not install_scripts and should_apply_user:
        warnings.append("use --install-scripts after changing OpenWrt scripts")
    return {
        "generated_at": now(),
        "apply": False,
        "summary": {
            "global_routes": pbr_plan["route_count"],
            "user_routes": user_plan["route_count"],
            "apply_global": should_apply_pbr,
            "apply_user": should_apply_user,
            "pbr_uploads": pbr_plan["upload_count"],
            "user_uploads": user_plan["upload_count"],
            "warnings": len(warnings),
        },
        "operator_command": "python tools\\vpn_control_app.py deploy-routes --apply",
        "pbr": {"manifest": pbr_manifest, "plan": pbr_plan, "will_apply": should_apply_pbr},
        "user_routes": {"manifest": user_manifest, "plan": user_plan, "will_apply": should_apply_user},
        "warnings": warnings,
    }


def load_cudy_ssh_password(explicit_password: str | None = None) -> str | None:
    if explicit_password:
        return explicit_password
    env_password = os.environ.get("CUDY_SSH_PASSWORD")
    if env_password:
        return env_password
    if DEFAULT_CUDY_PASSWORD_FILE.exists():
        password = DEFAULT_CUDY_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if password:
            return password
    return None


def apply_combined_route_deploy(
    db_path: Path,
    inventory_path: Path,
    *,
    ssh_host: str = DEFAULT_CUDY_HOST,
    ssh_user: str = DEFAULT_CUDY_USER,
    ssh_password: str,
    ssh_timeout: int = 60,
    pbr_output_dir: Path = DEFAULT_PBR_EXPORT_DIR,
    user_output_dir: Path = DEFAULT_USER_ROUTES_EXPORT_DIR,
    pbr_remote_dir: str = REMOTE_PBR_DIR,
    user_remote_dir: str = REMOTE_USER_ROUTES_DIR,
    install_scripts: bool = False,
    restart_pbr: bool = True,
    run_user_apply: bool = True,
    prune_empty: bool = False,
    allow_empty: bool = False,
) -> dict[str, Any]:
    preview = build_combined_deploy_preview(
        db_path,
        inventory_path,
        pbr_output_dir=pbr_output_dir,
        user_output_dir=user_output_dir,
        pbr_remote_dir=pbr_remote_dir,
        user_remote_dir=user_remote_dir,
        ssh_host=ssh_host,
        install_scripts=install_scripts,
        restart_pbr=restart_pbr,
        run_user_apply=run_user_apply,
        prune_empty=prune_empty,
        allow_empty=allow_empty,
    )
    should_apply_pbr = bool(preview["pbr"]["will_apply"])
    should_apply_user = bool(preview["user_routes"]["will_apply"])
    if not should_apply_pbr and not should_apply_user:
        raise ValueError("refusing to apply zero routes without allow_empty")

    results: dict[str, Any] = {"pbr": None, "user_routes": None}
    if should_apply_pbr:
        results["pbr"] = deploy_pbr_overrides(
            preview["pbr"]["manifest"],
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_password=ssh_password,
            ssh_timeout=ssh_timeout,
            remote_dir=pbr_remote_dir,
            install_scripts=install_scripts,
            restart_pbr=restart_pbr,
            prune_empty=prune_empty,
        )
    if should_apply_user:
        results["user_routes"] = deploy_user_routes(
            preview["user_routes"]["manifest"],
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_password=ssh_password,
            ssh_timeout=ssh_timeout,
            remote_dir=user_remote_dir,
            install_script=install_scripts,
            run_apply=run_user_apply,
        )
    return {
        "ok": True,
        "applied_at": now(),
        "ssh_host": ssh_host,
        "preview": preview,
        "results": results,
    }


def safe_interface_name(value: str | None) -> str | None:
    if not value:
        return None
    if not SAFE_INTERFACE_RE.match(value):
        return None
    return value


def export_pbr_overrides(db_path: Path, inventory_path: Path, output_dir: Path) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        servers = server_map(conn)
        cached_auto = auto_cache_map(conn)
        enabled_interfaces: set[str] = set()
        for server in servers.values():
            iface = safe_interface_name(server.get("interface"))
            if iface and iface != "wan" and bool(server.get("enabled")):
                enabled_interfaces.add(iface)
        global_routes = rows(
            conn,
            """
            SELECT domain, server_id
            FROM global_domain_routes
            WHERE enabled = 1
            ORDER BY domain
            """,
        )
        user_route_count = conn.execute(
            "SELECT count(*) FROM user_domain_routes WHERE enabled = 1"
        ).fetchone()[0]

    grouped: dict[str, set[str]] = {iface: set() for iface in enabled_interfaces}
    exported_routes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for route in global_routes:
        domain = route["domain"]
        requested_server_id = route["server_id"]
        auto_policy = None
        if requested_server_id == "auto":
            with connect(db_path) as conn:
                auto_policy = resolve_auto_candidate_policy(conn, user_id="", domain=domain)
        resolved_server_id, cached = resolve_route_server(
            domain=domain,
            requested_server_id=requested_server_id,
            servers=servers,
            auto_cache=cached_auto,
            auto_policy=auto_policy,
            context=domain,
            warnings=warnings,
        )
        if not resolved_server_id:
            continue
        server_id = resolved_server_id
        server = servers.get(server_id)
        if not server:
            warnings.append(f"{domain}: unknown server {server_id}; skipped")
            continue
        if not server.get("enabled"):
            warnings.append(f"{domain}: server {server_id} is disabled; skipped")
            continue
        iface = safe_interface_name(server.get("interface"))
        if not iface:
            warnings.append(f"{domain}: server {server_id} has no safe interface; skipped")
            continue
        kind = server.get("kind")
        if kind == "sing-box-profile":
            warnings.append(
                f"{domain}: {server_id} is a profile on shared interface {iface}; "
                "PBR export can only route to the current interface profile"
            )
        grouped.setdefault(iface, set()).add(domain)
        exported_routes.append(
            {
                "domain": domain,
                "server_id": server_id,
                "requested_server_id": requested_server_id,
                "interface": iface,
                "auto_status": cached.get("status") if cached else None,
                "auto_score_ms": cached.get("score_ms") if cached else None,
                "auto_candidate_policy": auto_policy,
            }
        )

    files: list[dict[str, Any]] = []
    for iface in sorted(grouped):
        path = output_dir / f"force-{iface}.domains"
        domains = sorted(grouped[iface])
        path.write_text("".join(f"{domain}\n" for domain in domains), encoding="utf-8", newline="\n")
        files.append({"path": str(path), "name": path.name, "interface": iface, "domains": len(domains)})

    if user_route_count:
        warnings.append(
            f"{user_route_count} enabled user-specific routes are not exported yet; "
            "they belong to the separate source-IP user-route layer"
        )

    manifest = {
        "generated_at": now(),
        "output_dir": str(output_dir),
        "mode": "global-pbr-overrides",
        "exported_routes": exported_routes,
        "files": files,
        "warnings": warnings,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def safe_route_group(index: int, iface: str) -> str:
    safe_iface = re.sub(r"[^A-Za-z0-9_.-]", "_", iface)
    return f"u{index:03d}_{safe_iface}"


def export_user_routes(db_path: Path, inventory_path: Path, output_dir: Path) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        servers = server_map(conn)
        cached_auto = auto_cache_map(conn)
        route_rows = rows(
            conn,
            """
            SELECT 'domain' AS target_type, r.domain AS target, r.domain, r.server_id,
                   u.id AS user_id, u.display_name, u.client_ip
            FROM user_domain_routes r
            JOIN users u ON u.id = r.user_id
            WHERE r.enabled = 1 AND u.enabled = 1 AND u.role = 'user'
            UNION ALL
            SELECT 'ip' AS target_type, r.target_cidr AS target, NULL AS domain, r.server_id,
                   u.id AS user_id, u.display_name, u.client_ip
            FROM user_ip_routes r
            JOIN users u ON u.id = r.user_id
            WHERE r.enabled = 1 AND u.enabled = 1 AND u.role = 'user'
            ORDER BY user_id, server_id, target
            """,
        )

    warnings: list[str] = []
    group_map: dict[tuple[str, str], str] = {}
    exported_routes: list[dict[str, Any]] = []
    tsv_lines = ["# group\tuser_id\tclient_ip\tinterface\ttarget\n"]

    for route in route_rows:
        user_id = route["user_id"]
        client_ip = normalize_client_ip(route["client_ip"]) if route["client_ip"] else None
        target_type = route["target_type"]
        target = route["target"]
        domain = route["domain"]
        server_id = route["server_id"]
        label = f"{target_type}:{target}"
        if not client_ip:
            warnings.append(f"{user_id}/{label}: missing client_ip; skipped")
            continue
        requested_server_id = server_id
        auto_policy = None
        route_key = domain or target
        if requested_server_id == "auto":
            route_key = domain if target_type == "domain" else auto_cache_key_for_ip_route(target)
            with connect(db_path) as conn:
                auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=route_key)
        resolved_server_id, cached = resolve_route_server(
            domain=route_key,
            requested_server_id=requested_server_id,
            servers=servers,
            auto_cache=cached_auto,
            auto_policy=auto_policy,
            context=f"{user_id}/{label}",
            warnings=warnings,
        )
        if not resolved_server_id:
            continue
        server_id = resolved_server_id
        server = servers.get(server_id)
        if not server:
            warnings.append(f"{user_id}/{label}: unknown server {server_id}; skipped")
            continue
        if not server.get("enabled"):
            warnings.append(f"{user_id}/{label}: server {server_id} is disabled; skipped")
            continue
        iface = safe_interface_name(server.get("interface"))
        if not iface:
            warnings.append(f"{user_id}/{label}: server {server_id} has no safe interface; skipped")
            continue
        if server.get("kind") == "sing-box-profile":
            warnings.append(
                f"{user_id}/{label}: {server_id} is a profile on shared interface {iface}; "
                "source routing can only select the currently active profile"
            )
        group_key = (client_ip, iface)
        group = group_map.get(group_key)
        if not group:
            group = safe_route_group(len(group_map) + 1, iface)
            group_map[group_key] = group
        tsv_lines.append(f"{group}\t{user_id}\t{client_ip}\t{iface}\t{target}\n")
        exported_routes.append(
            {
                "group": group,
                "user_id": user_id,
                "client_ip": client_ip,
                "target_type": target_type,
                "target": target,
                "domain": domain,
                "server_id": server_id,
                "requested_server_id": requested_server_id,
                "interface": iface,
                "auto_status": cached.get("status") if cached else None,
                "auto_score_ms": cached.get("score_ms") if cached else None,
                "auto_candidate_policy": auto_policy,
            }
        )

    routes_path = output_dir / "routes.tsv"
    routes_path.write_text("".join(tsv_lines), encoding="utf-8", newline="\n")
    manifest = {
        "generated_at": now(),
        "output_dir": str(output_dir),
        "mode": "user-source-ip-routes",
        "route_count": len(exported_routes),
        "group_count": len(group_map),
        "routes_file": str(routes_path),
        "exported_routes": exported_routes,
        "warnings": warnings,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def build_pbr_deploy_plan(
    manifest: dict[str, Any],
    *,
    ssh_host: str,
    remote_dir: str,
    install_scripts: bool,
    restart_pbr: bool,
    prune_empty: bool,
) -> dict[str, Any]:
    files = [
        item
        for item in manifest.get("files", [])
        if str(item.get("name", "")).endswith(".domains") and (prune_empty or int(item.get("domains") or 0) > 0)
    ]
    uploads = [
        {
            "local": item["path"],
            "remote": f"{remote_dir.rstrip('/')}/{item['name']}",
            "domains": item.get("domains", 0),
        }
        for item in files
    ]
    uploads.append(
        {
            "local": str(Path(manifest["output_dir"]) / "manifest.json"),
            "remote": f"{remote_dir.rstrip('/')}/manifest.json",
            "domains": None,
        }
    )
    warnings = list(manifest.get("warnings") or [])
    warnings.append(
        "deploy-pbr-overrides can write generated force-<interface>.domains files; "
        "existing force-wan/force-vpn/force-*.ips/force-*.urls are not modified"
    )
    if not prune_empty:
        warnings.append("empty generated domain files are not uploaded unless --prune-empty is used")
    if not install_scripts:
        warnings.append("remote PBR script is not updated unless --install-scripts is used")
    return {
        "ssh_host": ssh_host,
        "remote_dir": remote_dir,
        "route_count": len(manifest.get("exported_routes") or []),
        "upload_count": len(uploads),
        "uploads": uploads,
        "install_scripts": install_scripts,
        "restart_pbr": restart_pbr,
        "prune_empty": prune_empty,
        "warnings": warnings,
    }


def ssh_connect(host: str, user: str, password: str, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def ssh_exec_checked(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if rc:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    if err.strip():
        out += "\nSTDERR:\n" + err
    return out


def ssh_upload_file(client: paramiko.SSHClient, local_path: str | Path, remote_path: str, timeout: int) -> None:
    data = Path(local_path).read_bytes()
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not active")
    channel = transport.open_session(timeout=timeout)
    channel.settimeout(timeout)
    channel.exec_command(f"cat > {shlex.quote(remote_path)}")
    channel.sendall(data)
    channel.shutdown_write()
    stdout = channel.makefile("rb", -1).read().decode("utf-8", "replace")
    stderr = channel.makefile_stderr("rb", -1).read().decode("utf-8", "replace")
    rc = channel.recv_exit_status()
    channel.close()
    if rc:
        raise RuntimeError(
            f"remote upload failed rc={rc}: {remote_path}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )


def deploy_pbr_overrides(
    manifest: dict[str, Any],
    *,
    ssh_host: str,
    ssh_user: str,
    ssh_password: str,
    ssh_timeout: int,
    remote_dir: str,
    install_scripts: bool,
    restart_pbr: bool,
    prune_empty: bool,
) -> dict[str, Any]:
    plan = build_pbr_deploy_plan(
        manifest,
        ssh_host=ssh_host,
        remote_dir=remote_dir,
        install_scripts=install_scripts,
        restart_pbr=restart_pbr,
        prune_empty=prune_empty,
    )
    client = ssh_connect(ssh_host, ssh_user, ssh_password, ssh_timeout)
    try:
        backup_output = ssh_exec_checked(
            client,
            """
set -eu
backup="/root/backup-pbr-overrides/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
[ -d /etc/pbr-overrides ] && cp -a /etc/pbr-overrides "$backup/" || true
[ -f /usr/share/pbr/pbr.user.opencck-merged-vpn ] && cp /usr/share/pbr/pbr.user.opencck-merged-vpn "$backup/" || true
mkdir -p /etc/pbr-overrides /root/install
printf '%s\n' "$backup"
""",
            ssh_timeout,
        )
        backup_dir = backup_output.strip().splitlines()[-1]
        for upload in plan["uploads"]:
            ssh_upload_file(client, upload["local"], upload["remote"], ssh_timeout)
        if install_scripts:
            ssh_upload_file(client, LOCAL_PBR_SCRIPT, "/tmp/cudy-tr3000-pbr.user.opencck-merged-vpn", ssh_timeout)
            ssh_upload_file(client, LOCAL_SWITCHER_INSTALLER, "/root/install/install-vpn-switchers.sh", ssh_timeout)

        commands = [
            "chmod 644 /etc/pbr-overrides/*.domains /etc/pbr-overrides/manifest.json 2>/dev/null || true",
        ]
        if install_scripts:
            commands.extend(
                [
                    "sh -n /tmp/cudy-tr3000-pbr.user.opencck-merged-vpn",
                    "cp /tmp/cudy-tr3000-pbr.user.opencck-merged-vpn /usr/share/pbr/pbr.user.opencck-merged-vpn",
                    "chmod +x /usr/share/pbr/pbr.user.opencck-merged-vpn",
                    "sh -n /root/install/install-vpn-switchers.sh",
                    "chmod +x /root/install/install-vpn-switchers.sh",
                    "/root/install/install-vpn-switchers.sh",
                ]
            )
        if restart_pbr:
            commands.append(
                "if [ -x /usr/bin/cudy-pbr-safe-restart ]; then "
                "/usr/bin/cudy-pbr-safe-restart; else /etc/init.d/pbr restart; fi"
            )
            commands.append("/etc/init.d/pbr status 2>/dev/null | head -20 || true")
        apply_output = ssh_exec_checked(client, "set -eu\n" + "\n".join(commands) + "\n", ssh_timeout * 3)
        return {
            "ok": True,
            "backup_dir": backup_dir,
            "plan": plan,
            "output": apply_output.strip(),
        }
    finally:
        client.close()


def build_user_routes_deploy_plan(
    manifest: dict[str, Any],
    *,
    ssh_host: str,
    remote_dir: str,
    install_script: bool,
    run_apply: bool,
) -> dict[str, Any]:
    uploads = [
        {
            "local": manifest["routes_file"],
            "remote": f"{remote_dir.rstrip('/')}/routes.tsv",
        },
        {
            "local": str(Path(manifest["output_dir"]) / "manifest.json"),
            "remote": f"{remote_dir.rstrip('/')}/manifest.json",
        },
    ]
    warnings = list(manifest.get("warnings") or [])
    if not install_script:
        warnings.append("remote user-route apply script is not updated unless --install-script is used")
    return {
        "ssh_host": ssh_host,
        "remote_dir": remote_dir,
        "route_count": manifest.get("route_count", 0),
        "group_count": manifest.get("group_count", 0),
        "upload_count": len(uploads),
        "uploads": uploads,
        "install_script": install_script,
        "run_apply": run_apply,
        "warnings": warnings,
    }


def deploy_user_routes(
    manifest: dict[str, Any],
    *,
    ssh_host: str,
    ssh_user: str,
    ssh_password: str,
    ssh_timeout: int,
    remote_dir: str,
    install_script: bool,
    run_apply: bool,
) -> dict[str, Any]:
    plan = build_user_routes_deploy_plan(
        manifest,
        ssh_host=ssh_host,
        remote_dir=remote_dir,
        install_script=install_script,
        run_apply=run_apply,
    )
    client = ssh_connect(ssh_host, ssh_user, ssh_password, ssh_timeout)
    try:
        backup_output = ssh_exec_checked(
            client,
            """
set -eu
backup="/root/backup-cudy-user-routes/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
if [ -d /etc/cudy-user-routes ]; then
  mkdir -p "$backup/cudy-user-routes"
  [ -f /etc/cudy-user-routes/routes.tsv ] && cat /etc/cudy-user-routes/routes.tsv > "$backup/cudy-user-routes/routes.tsv" || true
  [ -f /etc/cudy-user-routes/manifest.json ] && cat /etc/cudy-user-routes/manifest.json > "$backup/cudy-user-routes/manifest.json" || true
fi
[ -f /usr/bin/cudy-user-routes-apply ] && cp /usr/bin/cudy-user-routes-apply "$backup/" || true
[ -f /etc/init.d/cudy-user-routes ] && cp /etc/init.d/cudy-user-routes "$backup/cudy-user-routes.init" || true
mkdir -p /etc/cudy-user-routes
printf '%s\n' "$backup"
""",
            ssh_timeout,
        )
        backup_dir = backup_output.strip().splitlines()[-1]
        for upload in plan["uploads"]:
            ssh_upload_file(client, upload["local"], upload["remote"], ssh_timeout)
        if install_script:
            ssh_upload_file(client, LOCAL_USER_ROUTES_APPLY, "/usr/bin/cudy-user-routes-apply", ssh_timeout)
            ssh_upload_file(client, LOCAL_USER_ROUTES_INIT, "/etc/init.d/cudy-user-routes", ssh_timeout)

        commands = [
            "chmod 644 /etc/cudy-user-routes/routes.tsv /etc/cudy-user-routes/manifest.json 2>/dev/null || true",
        ]
        if install_script:
            commands.extend(
                [
                    "sh -n /usr/bin/cudy-user-routes-apply",
                    "chmod +x /usr/bin/cudy-user-routes-apply",
                    "sh -n /etc/init.d/cudy-user-routes",
                    "chmod +x /etc/init.d/cudy-user-routes",
                    "/etc/init.d/cudy-user-routes enable",
                ]
            )
        if run_apply:
            commands.append("/usr/bin/cudy-user-routes-apply")
            commands.append("nft list table inet cudy_user_routes 2>/dev/null | sed -n '1,120p' || true")
        output = ssh_exec_checked(client, "set -eu\n" + "\n".join(commands) + "\n", ssh_timeout * 3)
        return {
            "ok": True,
            "backup_dir": backup_dir,
            "plan": plan,
            "output": output.strip(),
        }
    finally:
        client.close()


def collect_user_routes_status(
    *,
    ssh_host: str,
    ssh_user: str,
    ssh_password: str,
    ssh_timeout: int,
) -> dict[str, Any]:
    command = r"""
set -eu
printf '@@SECTION:routes_tsv@@\n'
sed -n '1,200p' /etc/cudy-user-routes/routes.tsv 2>/dev/null || true
printf '@@SECTION:manifest@@\n'
sed -n '1,240p' /etc/cudy-user-routes/manifest.json 2>/dev/null || true
printf '@@SECTION:nft_table@@\n'
nft list table inet cudy_user_routes 2>/dev/null || true
printf '@@SECTION:ip_rules@@\n'
ip rule show 2>/dev/null | grep 'lookup pbr_' || true
"""
    client = ssh_connect(ssh_host, ssh_user, ssh_password, ssh_timeout)
    try:
        raw = ssh_exec_checked(client, command, ssh_timeout)
    finally:
        client.close()

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("@@SECTION:") and line.endswith("@@"):
            current = line[len("@@SECTION:") : -2]
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {
        "ssh_host": ssh_host,
        "routes_tsv": "\n".join(sections.get("routes_tsv", [])).strip(),
        "manifest": "\n".join(sections.get("manifest", [])).strip(),
        "nft_table": "\n".join(sections.get("nft_table", [])).strip(),
        "ip_rules": "\n".join(sections.get("ip_rules", [])).strip(),
    }


def validate_server_id(conn: sqlite3.Connection, server_id: str, *, require_user_visible: bool) -> None:
    if server_id in {"auto", "direct"}:
        return
    if require_user_visible:
        value = row(conn, "SELECT id FROM servers WHERE id = ? AND enabled = 1 AND user_visible = 1", (server_id,))
    else:
        value = row(conn, "SELECT id FROM servers WHERE id = ?", (server_id,))
    if value is None:
        raise ValueError(f"Unknown or unavailable server: {server_id}")


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain):
        raise ValueError("Domain must look like example.com")
    return domain


def normalize_lookup_target(value: str) -> dict[str, str]:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Target is required")
    try:
        network = ipaddress.ip_network(raw, strict=False)
        if network.version != 4:
            raise ValueError("Only IPv4 targets are supported")
        return {"kind": "ip", "target": str(network)}
    except ValueError:
        pass
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname
    path_candidate = parsed.path.strip("/") if not host else ""
    candidate = host or path_candidate or raw
    candidate = candidate.strip().strip("[]").rstrip(".")
    try:
        network = ipaddress.ip_network(candidate, strict=False)
        if network.version != 4:
            raise ValueError("Only IPv4 targets are supported")
        return {"kind": "ip", "target": str(network)}
    except ValueError:
        pass
    return {"kind": "domain", "target": normalize_domain(candidate)}


def service_alias_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    entries = rows(
        conn,
        """
        SELECT alias, label, targets_json, updated_at
        FROM service_aliases
        ORDER BY label, alias
        """,
    )
    for entry in entries:
        entry["scope"] = "global"
        try:
            entry["targets"] = json.loads(entry.pop("targets_json") or "[]")
        except json.JSONDecodeError:
            entry["targets"] = []
    return entries


def user_service_alias_rows(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    entries = rows(
        conn,
        """
        SELECT alias, label, targets_json, updated_at
        FROM user_service_aliases
        WHERE user_id = ?
        ORDER BY label, alias
        """,
        (user_id,),
    )
    for entry in entries:
        entry["scope"] = "user"
        try:
            entry["targets"] = json.loads(entry.pop("targets_json") or "[]")
        except json.JSONDecodeError:
            entry["targets"] = []
    return entries


def effective_service_alias_rows(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    effective = {entry["alias"]: entry for entry in service_alias_rows(conn)}
    effective.update({entry["alias"]: entry for entry in user_service_alias_rows(conn, user_id=user_id)})
    return sorted(effective.values(), key=lambda entry: (str(entry.get("label") or "").casefold(), entry["alias"]))


def resolve_service_alias(conn: sqlite3.Connection, *, user_id: str, alias: str) -> dict[str, Any] | None:
    local = row(
        conn,
        """
        SELECT alias, label, targets_json, updated_at, 'user' AS scope
        FROM user_service_aliases
        WHERE user_id = ? AND alias = ?
        """,
        (user_id, alias),
    )
    if local:
        return local
    return row(
        conn,
        """
        SELECT alias, label, targets_json, updated_at, 'global' AS scope
        FROM service_aliases
        WHERE alias = ?
        """,
        (alias,),
    )


def save_service_alias(
    db_path: Path,
    inventory_path: Path,
    *,
    alias: str,
    label: str,
    targets: Any,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_alias = normalize_alias(alias)
    normalized_label = (label or normalized_alias).strip()[:80]
    normalized_targets = parse_alias_targets(targets)
    timestamp = now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO service_aliases (alias, label, targets_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(alias)
            DO UPDATE SET label = excluded.label,
                          targets_json = excluded.targets_json,
                          updated_at = excluded.updated_at
            """,
            (
                normalized_alias,
                normalized_label,
                json.dumps(normalized_targets, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
    return {"ok": True, "alias": normalized_alias, "label": normalized_label, "targets": normalized_targets}


def delete_service_alias(db_path: Path, inventory_path: Path, *, alias: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_alias = normalize_alias(alias)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM service_aliases WHERE alias = ?", (normalized_alias,))
    return {"ok": True, "alias": normalized_alias}


def save_user_service_alias(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    alias: str,
    label: str,
    targets: Any,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_alias = normalize_alias(alias)
    normalized_label = (label or normalized_alias).strip()[:80]
    normalized_targets = parse_alias_targets(targets)
    timestamp = now()
    with connect(db_path) as conn:
        if row(conn, "SELECT id FROM users WHERE id = ? AND enabled = 1", (user_id,)) is None:
            raise ValueError(f"Unknown or disabled user: {user_id}")
        conn.execute(
            """
            INSERT INTO user_service_aliases (user_id, alias, label, targets_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, alias)
            DO UPDATE SET label = excluded.label,
                          targets_json = excluded.targets_json,
                          updated_at = excluded.updated_at
            """,
            (
                user_id,
                normalized_alias,
                normalized_label,
                json.dumps(normalized_targets, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
    return {
        "ok": True,
        "user_id": user_id,
        "alias": normalized_alias,
        "label": normalized_label,
        "targets": normalized_targets,
        "scope": "user",
    }


def delete_user_service_alias(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    alias: str,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_alias = normalize_alias(alias)
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM user_service_aliases WHERE user_id = ? AND alias = ?",
            (user_id, normalized_alias),
        )
    return {"ok": True, "user_id": user_id, "alias": normalized_alias, "scope": "user"}


def normalize_critical_service_key(value: str) -> str:
    return normalize_alias(value)


def normalize_critical_service_target(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Critical service target cannot be empty")
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Critical service targets must use http:// or https://")
        if parsed.username or parsed.password:
            raise ValueError("Credentials are not allowed in critical service URLs")
        return raw
    normalized = normalize_lookup_target(raw)
    target = normalized["target"]
    if normalized["kind"] == "ip" and "/" in target:
        network = ipaddress.ip_network(target, strict=False)
        if network.prefixlen != 32:
            raise ValueError("Critical service IP targets must be individual hosts, not CIDR ranges")
        target = str(network.network_address)
    return f"https://{target}/"


def parse_critical_service_targets(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in re.split(r"[,;\r\n]+", value) if item.strip()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("targets must be a list or a comma-separated string")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_critical_service_target(item)
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    if not result:
        raise ValueError("Critical service targets cannot be empty")
    if len(result) > 20:
        raise ValueError("A critical service can contain at most 20 targets")
    return result


def normalize_content_pattern(value: str, *, field: str) -> str:
    pattern = (value or "").strip()
    if len(pattern) > 1000:
        raise ValueError(f"{field} must be at most 1000 characters")
    if pattern:
        try:
            re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"Invalid {field}: {exc}") from exc
    return pattern


def critical_service_rows(conn: sqlite3.Connection, *, user_id: str | None = None) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = ()
    where = ""
    if user_id is not None:
        where = "WHERE user_id = ?"
        params = (user_id,)
    entries = rows(
        conn,
        f"""
        SELECT user_id, service_key, label, targets_json, success_pattern,
               failure_pattern, routing_enabled, candidate_server_ids,
               enabled, created_at, updated_at
        FROM critical_services
        {where}
        ORDER BY CASE WHEN user_id = '' THEN 0 ELSE 1 END, user_id, label, service_key
        """,
        params,
    )
    for entry in entries:
        try:
            entry["targets"] = json.loads(entry.pop("targets_json") or "[]")
        except json.JSONDecodeError:
            entry["targets"] = []
        try:
            entry["candidate_server_ids"] = json.loads(entry.get("candidate_server_ids") or "[]")
        except json.JSONDecodeError:
            entry["candidate_server_ids"] = []
        entry["enabled"] = bool(entry.get("enabled"))
        entry["routing_enabled"] = bool(entry.get("routing_enabled"))
        entry["scope"] = "global" if not entry.get("user_id") else "user"
    return entries


def effective_critical_services(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    effective: dict[str, dict[str, Any]] = {
        item["service_key"]: item
        for item in critical_service_rows(conn, user_id="")
        if item.get("enabled")
    }
    for item in critical_service_rows(conn, user_id=user_id):
        if item.get("enabled"):
            effective[item["service_key"]] = item
        else:
            effective.pop(item["service_key"], None)
    return sorted(effective.values(), key=lambda item: (str(item.get("label") or "").lower(), item["service_key"]))


def critical_service_target_hosts(service: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for target in service.get("targets") or []:
        parsed = urlparse(str(target))
        try:
            host = normalize_domain(parsed.hostname or "")
        except ValueError:
            continue
        if host not in seen:
            result.append(host)
            seen.add(host)
    return result


def service_auto_cache_key(user_id: str, service_key: str) -> str:
    identity = f"{user_id}:{service_key}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"service-{digest}.group.local"


def service_group_policy(conn: sqlite3.Connection, service: dict[str, Any]) -> dict[str, Any]:
    candidates = list(service.get("candidate_server_ids") or [])
    return {
        "user_id": service.get("user_id") or "",
        "domain": service_auto_cache_key(str(service.get("user_id") or ""), str(service["service_key"])),
        "candidate_server_ids": candidates,
        "expanded_candidate_server_ids": expand_auto_candidate_ids(server_map(conn), candidates),
        "scope": "user_service_group" if service.get("user_id") else "global_service_group",
        "service_key": service["service_key"],
        "label": service.get("label") or service["service_key"],
    }


def effective_service_group_for_domain(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    domain: str,
) -> dict[str, Any] | None:
    try:
        normalized_domain = normalize_domain(domain)
    except ValueError:
        return None
    for service in effective_critical_services(conn, user_id=user_id):
        if not service.get("routing_enabled"):
            continue
        for host in critical_service_target_hosts(service):
            if normalized_domain == host or normalized_domain.endswith("." + host):
                return service
    return None


def critical_probe_patterns(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    domain: str,
    url: str = "",
) -> dict[str, str]:
    empty = {"success_pattern": "", "failure_pattern": ""}
    try:
        host = normalize_domain(domain)
    except ValueError:
        # Synthetic cache keys used for CIDR probes are not public domains and
        # cannot have meaningful HTTP content rules.
        return empty
    if url:
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            return empty
        if parsed.hostname:
            try:
                host = normalize_domain(parsed.hostname)
            except ValueError:
                return empty
    for service in effective_critical_services(conn, user_id=user_id):
        for target in service.get("targets") or []:
            parsed = urlparse(str(target))
            target_host = normalize_domain(parsed.hostname or "")
            if not target_host:
                continue
            if host == target_host or host.endswith("." + target_host) or target_host.endswith("." + host):
                return {
                    "success_pattern": str(service.get("success_pattern") or ""),
                    "failure_pattern": str(service.get("failure_pattern") or ""),
                }
    return empty


def save_critical_service(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    service_key: str,
    label: str,
    targets: Any,
    success_pattern: str = "",
    failure_pattern: str = "",
    routing_enabled: bool = False,
    candidate_server_ids: Any = None,
    enabled: bool = True,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_key = normalize_critical_service_key(service_key or label)
    normalized_label = (label or normalized_key).strip()[:80]
    normalized_targets = parse_critical_service_targets(targets)
    success_pattern = normalize_content_pattern(success_pattern, field="success pattern")
    failure_pattern = normalize_content_pattern(failure_pattern, field="failure pattern")
    candidates: list[str] = []
    if candidate_server_ids not in (None, "", []):
        candidates = parse_candidate_server_ids(candidate_server_ids)
    if routing_enabled and not candidates:
        raise ValueError("A routed service group requires at least one Auto candidate")
    timestamp = now()
    with connect(db_path) as conn:
        if user_id and row(conn, "SELECT id FROM users WHERE id = ?", (user_id,)) is None:
            raise ValueError(f"Unknown user: {user_id}")
        for server_id in candidates:
            if server_id != AUTO_ALL_REST:
                validate_server_id(conn, server_id, require_user_visible=True)
        conn.execute(
            """
            INSERT INTO critical_services (
              user_id, service_key, label, targets_json, success_pattern,
              failure_pattern, routing_enabled, candidate_server_ids,
              enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, service_key) DO UPDATE SET
              label = excluded.label,
              targets_json = excluded.targets_json,
              success_pattern = excluded.success_pattern,
              failure_pattern = excluded.failure_pattern,
              routing_enabled = excluded.routing_enabled,
              candidate_server_ids = excluded.candidate_server_ids,
              enabled = excluded.enabled,
              updated_at = excluded.updated_at
            """,
            (
                user_id,
                normalized_key,
                normalized_label,
                json.dumps(normalized_targets, ensure_ascii=False),
                success_pattern,
                failure_pattern,
                int(bool(routing_enabled)),
                json.dumps(candidates, ensure_ascii=False),
                int(enabled),
                timestamp,
                timestamp,
            ),
        )
    return {
        "ok": True,
        "user_id": user_id,
        "service_key": normalized_key,
        "label": normalized_label,
        "targets": normalized_targets,
        "success_pattern": success_pattern,
        "failure_pattern": failure_pattern,
        "routing_enabled": bool(routing_enabled),
        "candidate_server_ids": candidates,
        "enabled": bool(enabled),
    }


def delete_critical_service(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    service_key: str,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_key = normalize_critical_service_key(service_key)
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM critical_services WHERE user_id = ? AND service_key = ?",
            (user_id, normalized_key),
        )
    return {"ok": True, "user_id": user_id, "service_key": normalized_key}


def json_list_append(raw: str, value: str) -> list[str]:
    try:
        items = [str(item) for item in json.loads(raw or "[]") if str(item)]
    except json.JSONDecodeError:
        items = []
    if value and value not in items:
        items.append(value)
    return items


def record_domain_discovery(
    conn: sqlite3.Connection,
    *,
    domain: str,
    user_id: str = "",
    client_ip: str = "",
    source: str = "route_lookup",
    note: str = "",
) -> dict[str, Any]:
    normalized_domain = normalize_domain(domain)
    timestamp = now()
    existing = row(conn, "SELECT * FROM domain_discovery_queue WHERE domain = ?", (normalized_domain,))
    if existing:
        user_ids = json_list_append(existing.get("user_ids_json") or "[]", user_id.strip())
        client_ips = json_list_append(existing.get("client_ips_json") or "[]", client_ip.strip())
        conn.execute(
            """
            UPDATE domain_discovery_queue
            SET last_seen_at = ?,
                hit_count = hit_count + 1,
                user_ids_json = ?,
                client_ips_json = ?,
                source = CASE WHEN source = '' THEN ? ELSE source END,
                note = CASE WHEN ? != '' THEN ? ELSE note END
            WHERE domain = ?
            """,
            (
                timestamp,
                json.dumps(user_ids, ensure_ascii=False),
                json.dumps(client_ips, ensure_ascii=False),
                source.strip(),
                note.strip(),
                note.strip(),
                normalized_domain,
            ),
        )
    else:
        user_ids = [user_id.strip()] if user_id.strip() else []
        client_ips = [client_ip.strip()] if client_ip.strip() else []
        conn.execute(
            """
            INSERT INTO domain_discovery_queue (
              domain, status, source, first_seen_at, last_seen_at, hit_count,
              user_ids_json, client_ips_json, note
            ) VALUES (?, 'pending', ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                normalized_domain,
                source.strip(),
                timestamp,
                timestamp,
                json.dumps(user_ids, ensure_ascii=False),
                json.dumps(client_ips, ensure_ascii=False),
                note.strip(),
            ),
        )
    return domain_discovery_item(conn, normalized_domain)


def domain_discovery_item(conn: sqlite3.Connection, domain: str) -> dict[str, Any]:
    item = row(conn, "SELECT * FROM domain_discovery_queue WHERE domain = ?", (normalize_domain(domain),))
    if not item:
        raise ValueError(f"Unknown discovered domain: {domain}")
    try:
        item["user_ids"] = json.loads(item.pop("user_ids_json") or "[]")
    except json.JSONDecodeError:
        item["user_ids"] = []
    try:
        item["client_ips"] = json.loads(item.pop("client_ips_json") or "[]")
    except json.JSONDecodeError:
        item["client_ips"] = []
    return item


def domain_discovery_rows(conn: sqlite3.Connection, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE status = ?"
        params.append(status)
    entries = rows(
        conn,
        f"""
        SELECT *
        FROM domain_discovery_queue
        {where}
        ORDER BY last_seen_at DESC, hit_count DESC, domain
        LIMIT ?
        """,
        (*params, limit),
    )
    result: list[dict[str, Any]] = []
    for item in entries:
        try:
            item["user_ids"] = json.loads(item.pop("user_ids_json") or "[]")
        except json.JSONDecodeError:
            item["user_ids"] = []
        try:
            item["client_ips"] = json.loads(item.pop("client_ips_json") or "[]")
        except json.JSONDecodeError:
            item["client_ips"] = []
        result.append(item)
    return result


def save_domain_discovery_status(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    status: str,
    note: str = "",
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_status = status.strip().lower()
    if normalized_status not in {"pending", "reviewed", "ignored", "promoted"}:
        raise ValueError("status must be pending, reviewed, ignored, or promoted")
    with connect(db_path) as conn:
        normalized_domain = normalize_domain(domain)
        cursor = conn.execute(
            """
            UPDATE domain_discovery_queue
            SET status = ?, note = CASE WHEN ? != '' THEN ? ELSE note END
            WHERE domain = ?
            """,
            (normalized_status, note.strip(), note.strip(), normalized_domain),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown discovered domain: {normalized_domain}")
        return {"ok": True, "item": domain_discovery_item(conn, normalized_domain)}


def promote_domain_discovery_to_auto_route(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    user_id: str = "",
    candidate_server_ids: Any = None,
    note: str = "",
    probe_now: bool = False,
    max_probe_candidates: int = 4,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    normalized_domain = normalize_domain(domain)
    normalized_user_id = (user_id or "").strip()
    normalized_candidates: list[str] | None = None
    if candidate_server_ids not in (None, "", []):
        normalized_candidates = parse_candidate_server_ids(candidate_server_ids)
    timestamp = now()
    with connect(db_path) as conn:
        discovery = domain_discovery_item(conn, normalized_domain)
        validate_server_id(conn, "auto", require_user_visible=True)
        if normalized_user_id:
            if row(conn, "SELECT id FROM users WHERE id = ?", (normalized_user_id,)) is None:
                raise ValueError(f"Unknown user: {normalized_user_id}")
        if normalized_candidates is not None:
            for server_id in normalized_candidates:
                if server_id != AUTO_ALL_REST:
                    validate_server_id(conn, server_id, require_user_visible=True)
        if normalized_user_id:
            conn.execute(
                """
                INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
                VALUES (?, ?, 'auto', 1, ?, ?)
                ON CONFLICT(user_id, domain)
                DO UPDATE SET server_id = 'auto', enabled = 1, updated_at = excluded.updated_at
                """,
                (normalized_user_id, normalized_domain, timestamp, timestamp),
            )
            route_scope = "user_domain"
        else:
            conn.execute(
                """
                INSERT INTO global_domain_routes (domain, server_id, enabled, created_at, updated_at)
                VALUES (?, 'auto', 1, ?, ?)
                ON CONFLICT(domain)
                DO UPDATE SET server_id = 'auto', enabled = 1, updated_at = excluded.updated_at
                """,
                (normalized_domain, timestamp, timestamp),
            )
            route_scope = "global_domain"
        mark_note = note.strip() or f"Promoted to {route_scope} Auto route"
        conn.execute(
            """
            UPDATE domain_discovery_queue
            SET status = 'promoted', note = ?, last_seen_at = ?
            WHERE domain = ?
            """,
            (mark_note, timestamp, normalized_domain),
        )
        auto_candidate_policy = None
        if normalized_candidates is not None:
            conn.execute(
                """
                INSERT INTO auto_candidate_policies (
                  user_id, domain, candidate_server_ids, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, domain)
                DO UPDATE SET candidate_server_ids = excluded.candidate_server_ids,
                              enabled = 1,
                              updated_at = excluded.updated_at
                """,
                (
                    normalized_user_id,
                    normalized_domain,
                    json.dumps(normalized_candidates, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            auto_candidate_policy = {
                "ok": True,
                "user_id": normalized_user_id,
                "domain": normalized_domain,
                "scope": auto_policy_scope(normalized_user_id, normalized_domain),
                "candidate_server_ids": normalized_candidates,
                "enabled": True,
                "updated_at": timestamp,
            }
        promoted = domain_discovery_item(conn, normalized_domain)
    probe_job = None
    if probe_now:
        probe_job = enqueue_auto_probe_for_domain(
            db_path,
            inventory_path,
            domain=normalized_domain,
            user_id=normalized_user_id,
            candidate_server_ids=normalized_candidates,
            max_candidates=max_probe_candidates,
        )
    return {
        "ok": True,
        "domain": normalized_domain,
        "user_id": normalized_user_id,
        "route_scope": route_scope,
        "server_id": "auto",
        "discovery": promoted,
        "previous_discovery": discovery,
        "auto_candidate_policy": auto_candidate_policy,
        "probe_job": probe_job,
    }


def domain_rule_for_user(conn: sqlite3.Connection, *, user_id: str, domain: str) -> dict[str, Any] | None:
    user_route = row(
        conn,
        """
        SELECT domain, server_id, enabled, updated_at, 'user' AS source
        FROM user_domain_routes
        WHERE user_id = ? AND domain = ? AND enabled = 1
        """,
        (user_id, domain),
    )
    if user_route:
        return user_route
    return row(
        conn,
        """
        SELECT domain, server_id, enabled, updated_at, 'global' AS source
        FROM global_domain_routes
        WHERE domain = ? AND enabled = 1
        """,
        (domain,),
    )


def ip_rule_for_user(conn: sqlite3.Connection, *, user_id: str, target: str) -> dict[str, Any] | None:
    network = ipaddress.ip_network(target, strict=False)
    address = network.network_address
    candidates = rows(
        conn,
        """
        SELECT user_id, target_cidr, server_id, enabled, updated_at, 'user' AS source
        FROM user_ip_routes
        WHERE user_id = ? AND enabled = 1
        UNION ALL
        SELECT '' AS user_id, target_cidr, server_id, enabled, updated_at, 'global' AS source
        FROM global_ip_routes
        WHERE enabled = 1
        """,
        (user_id,),
    )
    best: tuple[int, int, dict[str, Any]] | None = None
    for item in candidates:
        route_network = ipaddress.ip_network(item["target_cidr"], strict=False)
        if address not in route_network:
            continue
        source_rank = 1 if item["source"] == "user" else 0
        current = (route_network.prefixlen, source_rank, item)
        if best is None or current[:2] > best[:2]:
            best = current
    return best[2] if best else None


def resolve_lookup_rule(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    target_info: dict[str, str],
    servers: dict[str, dict[str, Any]],
    cached_auto: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    warnings: list[str] = []
    kind = target_info["kind"]
    target = target_info["target"]
    rule = domain_rule_for_user(conn, user_id=user_id, domain=target) if kind == "domain" else ip_rule_for_user(conn, user_id=user_id, target=target)
    service = effective_service_group_for_domain(conn, user_id=user_id, domain=target) if kind == "domain" else None
    service_policy = None
    if service and (rule is None or (rule.get("source") == "global" and service.get("scope") == "user")):
        service_policy = service_group_policy(conn, service)
        rule = {
            "domain": target,
            "server_id": "auto",
            "enabled": 1,
            "updated_at": service.get("updated_at") or "",
            "source": f"{service.get('scope')}_service_group",
            "service_key": service["service_key"],
            "service_label": service.get("label") or service["service_key"],
        }
    cache_key = ""
    if kind == "ip":
        cache_key = auto_cache_key_for_ip_route((rule or {}).get("target_cidr") or target)
    else:
        cache_key = (
            service_auto_cache_key(str(service.get("user_id") or ""), str(service["service_key"]))
            if service_policy
            else target
        )
    if rule:
        requested_server_id = str(rule["server_id"])
        auto_policy = service_policy
        if requested_server_id == "auto" and auto_policy is None:
            auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=cache_key)
        resolved_server_id, cached = resolve_route_server(
            domain=cache_key,
            requested_server_id=requested_server_id,
            servers=servers,
            auto_cache=cached_auto,
            auto_policy=auto_policy,
            context=f"{user_id}/{target}",
            warnings=warnings,
        )
        server_id = resolved_server_id or requested_server_id
        return {
            "target": target,
            "kind": kind,
            "route_state": "managed",
            "matched_rule": rule,
            "requested_server_id": requested_server_id,
            "server_id": server_id,
            "resolved_server_id": resolved_server_id,
            "server": compact_server(servers.get(server_id)),
            "auto_cache_key": cache_key if requested_server_id == "auto" else "",
            "auto_cache": cached,
            "auto_candidate_policy": auto_policy,
            "warnings": warnings,
        }

    default_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=cache_key)
    return {
        "target": target,
        "kind": kind,
        "route_state": "direct",
        "matched_rule": None,
        "requested_server_id": "direct",
        "server_id": "direct",
        "resolved_server_id": None,
        "server": None,
        "auto_cache_key": cache_key,
        "auto_cache": cached_auto.get(cache_key),
        "auto_candidate_policy": default_policy,
        "warnings": ["No managed route matches this target; traffic stays on the normal/direct route."],
    }


def route_lookup(db_path: Path, inventory_path: Path, *, user_id: str, target: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    target_raw = (target or "").strip()
    with connect(db_path) as conn:
        user = row(conn, "SELECT id, display_name, default_server_id, client_ip FROM users WHERE id = ? AND enabled = 1", (user_id,))
        if not user:
            raise ValueError(f"Unknown or disabled user: {user_id}")
        alias_key = ""
        try:
            alias_key = normalize_alias(target_raw)
        except ValueError:
            alias_key = ""
        alias = resolve_service_alias(conn, user_id=user_id, alias=alias_key) if alias_key else None
        servers = server_map(conn)
        cached_auto = auto_cache_map(conn)
        expanded_targets: list[dict[str, str]]
        alias_info = None
        if alias:
            try:
                raw_targets = json.loads(alias.get("targets_json") or "[]")
            except json.JSONDecodeError:
                raw_targets = []
            expanded_targets = [normalize_lookup_target(str(item)) for item in raw_targets]
            alias_info = {
                "alias": alias["alias"],
                "label": alias["label"],
                "targets": raw_targets,
                "scope": alias.get("scope") or "global",
            }
        else:
            expanded_targets = [normalize_lookup_target(target_raw)]
        results = [
            resolve_lookup_rule(
                conn,
                user_id=user_id,
                target_info=item,
                servers=servers,
                cached_auto=cached_auto,
            )
            for item in expanded_targets
        ]
        for item in results:
            if item.get("kind") == "domain" and item.get("route_state") == "direct":
                item["discovery"] = record_domain_discovery(
                    conn,
                    domain=str(item.get("target") or ""),
                    user_id=user_id,
                    client_ip=str(user.get("client_ip") or ""),
                    source="route_lookup",
                )
    return {
        "ok": True,
        "user": user,
        "input": target_raw,
        "alias": alias_info,
        "results": results,
    }


def lookup_cache_keys_for_target(conn: sqlite3.Connection, target: str) -> list[str]:
    target_raw = (target or "").strip()
    if not target_raw:
        return []
    alias_key = ""
    try:
        alias_key = normalize_alias(target_raw)
    except ValueError:
        alias_key = ""
    alias = row(conn, "SELECT targets_json FROM service_aliases WHERE alias = ?", (alias_key,)) if alias_key else None
    raw_targets: list[str]
    if alias:
        try:
            raw_targets = [str(item) for item in json.loads(alias.get("targets_json") or "[]")]
        except json.JSONDecodeError:
            raw_targets = []
    else:
        raw_targets = [target_raw]
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_targets:
        try:
            info = normalize_lookup_target(raw)
        except ValueError:
            continue
        key = auto_cache_key_for_ip_route(info["target"]) if info["kind"] == "ip" else info["target"]
        if key not in seen:
            result.append(key)
            seen.add(key)
    return result


def speed_mbps_from_check(check: dict[str, Any]) -> float | None:
    for key in ("speed_mbps", "download_mbps", "mbps"):
        value = check.get(key)
        if value not in (None, ""):
            try:
                return round(float(value), 2)
            except (TypeError, ValueError):
                pass
    for key in ("speed_Bps", "speed_download"):
        value = check.get(key)
        if value not in (None, ""):
            try:
                return round(float(value) * 8 / 1_000_000, 2)
            except (TypeError, ValueError):
                pass
    size = check.get("size_download")
    total = check.get("time_total")
    if size not in (None, "", "0") and total not in (None, "", "0"):
        try:
            return round(float(size) * 8 / float(total) / 1_000_000, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def probe_check_failure_reason(check: dict[str, Any]) -> str:
    for key in ("semantic_status", "resolve_status", "error"):
        value = str(check.get(key) or "").strip()
        if value and value not in {"ok", "resolved"}:
            return value
    http_code = check.get("http_code")
    if http_code not in (None, "", 0, "0"):
        return f"http_{http_code}"
    return "failed"


def recent_auto_winners(db_path: Path, inventory_path: Path, *, target: str = "", limit: int = 10) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    limit = max(1, min(int(limit), 50))
    with connect(db_path) as conn:
        keys = lookup_cache_keys_for_target(conn, target)
        params: list[Any] = []
        where = "status = 'done' AND winner_server_id IS NOT NULL"
        if keys:
            where += " AND domain IN (%s)" % ",".join("?" for _ in keys)
            params.extend(keys)
        entries = rows(
            conn,
            f"""
            SELECT id, domain, user_id, candidate_server_ids, claimed_by_device_id,
                   winner_server_id, score_ms, result_json, updated_at, finished_at
            FROM agent_probe_jobs
            WHERE {where}
            ORDER BY COALESCE(finished_at, updated_at) DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        failed_entries = rows(
            conn,
            f"""
            SELECT id, domain, user_id, candidate_server_ids, claimed_by_device_id,
                   error, result_json, updated_at, finished_at
            FROM agent_probe_jobs
            WHERE {where.replace("status = 'done' AND winner_server_id IS NOT NULL", "status = 'failed'")}
            ORDER BY COALESCE(finished_at, updated_at) DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        cache_params: list[Any] = []
        cache_where = "selected_server_id IS NOT NULL"
        if keys:
            cache_where += " AND domain IN (%s)" % ",".join("?" for _ in keys)
            cache_params.extend(keys)
        cache_entries = rows(
            conn,
            f"""
            SELECT domain, selected_server_id, score_ms, status, checked_at, metadata_json
            FROM domain_auto_cache
            WHERE {cache_where}
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (*cache_params, limit),
        )
    result: list[dict[str, Any]] = []
    seen_cache_keys: set[tuple[str, str]] = set()
    for item in entries:
        try:
            payload = json.loads(item.pop("result_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        winner = payload.get("winner") if isinstance(payload.get("winner"), dict) else {}
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        ok_checks = [check for check in checks if isinstance(check, dict) and check.get("ok")]
        try:
            candidates = json.loads(item.get("candidate_server_ids") or "[]")
        except json.JSONDecodeError:
            candidates = []
        result.append(
            {
                **item,
                "candidate_server_ids": candidates,
                "winner": winner,
                "latency_ms": winner.get("time_total_ms") or winner.get("elapsed_ms") or item.get("score_ms"),
                "speed_mbps": speed_mbps_from_check(winner),
                "remote_ip": winner.get("remote_ip") or "",
                "ok_candidates": len(ok_checks),
                "checks": checks,
            }
        )
        if item.get("domain") and item.get("winner_server_id"):
            seen_cache_keys.add((str(item["domain"]), str(item["winner_server_id"])))
    for item in cache_entries:
        if len(result) >= limit:
            break
        cache_key = (str(item.get("domain") or ""), str(item.get("selected_server_id") or ""))
        if cache_key in seen_cache_keys:
            continue
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        candidates = metadata.get("candidate_server_ids") or metadata.get("candidate_server_ids_expanded") or []
        if not isinstance(candidates, list):
            candidates = []
        result.append(
            {
                "id": None,
                "domain": item.get("domain") or "",
                "user_id": metadata.get("user_id") or "",
                "candidate_server_ids": candidates,
                "claimed_by_device_id": metadata.get("device_id") or "",
                "winner_server_id": item.get("selected_server_id") or "",
                "score_ms": item.get("score_ms"),
                "updated_at": item.get("checked_at"),
                "finished_at": item.get("checked_at"),
                "winner": {
                    "server_id": item.get("selected_server_id") or "",
                    "time_total_ms": item.get("score_ms"),
                    "source": "auto_cache",
                },
                "latency_ms": item.get("score_ms"),
                "speed_mbps": None,
                "remote_ip": metadata.get("remote_ip") or "",
                "ok_candidates": metadata.get("checked_candidates") or 0,
                "checks": [],
                "source": "auto_cache",
                "status": item.get("status") or "",
            }
        )
    failures: list[dict[str, Any]] = []
    for item in failed_entries:
        try:
            payload = json.loads(item.pop("result_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        try:
            candidates = json.loads(item.get("candidate_server_ids") or "[]")
        except json.JSONDecodeError:
            candidates = []
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        check_failures = [
            {
                "server_id": str(check.get("server_id") or ""),
                "reason": probe_check_failure_reason(check),
                "latency_ms": check.get("time_total_ms") or check.get("elapsed_ms"),
                "http_code": check.get("http_code"),
            }
            for check in checks
            if isinstance(check, dict) and not check.get("ok")
        ]
        failures.append(
            {
                **item,
                "candidate_server_ids": candidates,
                "reason": str(item.get("error") or payload.get("error") or "no working candidate"),
                "checks": check_failures,
            }
        )
    return {"ok": True, "target": target, "cache_keys": keys, "winners": result, "failures": failures}


class App:
    def __init__(self, db_path: Path, inventory_path: Path):
        self.db_path = db_path
        self.inventory_path = inventory_path
        self.agent_token_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.agent_token_denied_cache: dict[str, tuple[float, str]] = {}
        self.agent_token_cache_lock = threading.RLock()
        init_db(db_path, inventory_path)

    def conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def cached_agent(self, token: str) -> dict[str, Any] | None:
        now_epoch = time.time()
        with self.agent_token_cache_lock:
            cached = self.agent_token_cache.get(token)
            if not cached:
                return None
            expires_at, device = cached
            if expires_at <= now_epoch:
                self.agent_token_cache.pop(token, None)
                return None
            return dict(device)

    def agent_token_is_denied(self, token: str) -> bool:
        now_epoch = time.time()
        with self.agent_token_cache_lock:
            denied = self.agent_token_denied_cache.get(token)
            if not denied:
                return False
            expires_at, _device_id = denied
            if expires_at <= now_epoch:
                self.agent_token_denied_cache.pop(token, None)
                return False
            return True

    def cache_agent(self, token: str, device: dict[str, Any]) -> None:
        with self.agent_token_cache_lock:
            self.agent_token_cache[token] = (time.time() + AGENT_TOKEN_CACHE_SECONDS, dict(device))

    def invalidate_agent(self, device_id: str, *, deny_cached: bool) -> int:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            return 0
        with self.agent_token_cache_lock:
            stale_tokens = [
                token
                for token, (_expires_at, device) in self.agent_token_cache.items()
                if str(device.get("id") or "") == normalized_device_id
            ]
            for token in stale_tokens:
                self.agent_token_cache.pop(token, None)
                if deny_cached:
                    self.agent_token_denied_cache[token] = (
                        time.time() + AGENT_TOKEN_CACHE_SECONDS,
                        normalized_device_id,
                    )
            if not deny_cached:
                denied_tokens = [
                    token
                    for token, (_expires_at, denied_device_id) in self.agent_token_denied_cache.items()
                    if denied_device_id == normalized_device_id
                ]
                for token in denied_tokens:
                    self.agent_token_denied_cache.pop(token, None)
            return len(stale_tokens)


class Handler(BaseHTTPRequestHandler):
    server_version = "VpnControl/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_file(self, path: Path, *, download_name: str) -> None:
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-disposition", f'attachment; filename="{download_name}"')
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_binary_file(self, path: Path, *, download_name: str, content_type: str = "application/octet-stream") -> None:
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-disposition", f'attachment; filename="{download_name}"')
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(
        self,
        data: Any,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        accepts_gzip = "gzip" in self.headers.get("accept-encoding", "").lower()
        content_encoding = ""
        if accepts_gzip and len(payload) >= 1024:
            payload = gzip.compress(payload, compresslevel=5)
            content_encoding = "gzip"
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("vary", "accept-encoding")
        if content_encoding:
            self.send_header("content-encoding", content_encoding)
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, error: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"error": error}, status)

    def send_redirect(self, location: str, *, extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("location", location)
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.send_header("content-length", "0")
        self.end_headers()

    def cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("cookie", "")
        for item in raw.split(";"):
            if "=" not in item:
                continue
            key, value = item.strip().split("=", 1)
            if key == name:
                return value
        return None

    def current_user(self) -> dict[str, Any] | None:
        token = self.cookie_value(SESSION_COOKIE)
        with self.app.conn() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
            if token:
                session_user = row(
                    conn,
                    """
                    SELECT u.id, u.display_name, u.role, u.default_server_id, u.client_ip, u.enabled
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.token = ? AND s.expires_at > ? AND u.enabled = 1
                    """,
                    (token, int(time.time())),
                )
                if session_user is not None:
                    return session_user

            remote_ip = normalize_client_ip_or_none(self.client_address[0])
            if remote_ip:
                return row(
                    conn,
                    """
                    SELECT id, display_name, role, default_server_id, client_ip, enabled
                    FROM users
                    WHERE client_ip = ? AND enabled = 1
                    """,
                    (remote_ip,),
                )
        return None

    def require_user(self) -> dict[str, Any]:
        user = self.current_user()
        if user is None:
            raise PermissionError("Authentication required")
        return user

    def require_admin(self) -> dict[str, Any]:
        user = self.require_user()
        if user.get("role") != "admin":
            raise PermissionError("Admin role required")
        return user

    def agent_token(self) -> str | None:
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        token = self.headers.get("x-device-token", "").strip()
        return token or None

    def agent_from_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        if self.app.agent_token_is_denied(token):
            return None
        cached = self.app.cached_agent(token)
        if cached is not None:
            return cached
        remote_ip = normalize_client_ip_or_none(self.client_address[0])
        user_agent = self.headers.get("user-agent", "")[:240]
        with self.app.conn() as conn:
            devices = rows(
                conn,
                """
                SELECT d.id, d.user_id, d.display_name, d.platform, d.token_salt,
                       d.token_hash, d.enabled, u.enabled AS user_enabled
                FROM agent_devices d
                JOIN users u ON u.id = d.user_id
                WHERE d.enabled = 1 AND u.enabled = 1
                """,
            )
            for device in devices:
                if verify_device_token(token, device.get("token_salt"), device.get("token_hash")):
                    timestamp = now()
                    conn.execute(
                        """
                        UPDATE agent_devices
                        SET last_seen_at = ?, last_ip = ?, last_user_agent = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (timestamp, remote_ip, user_agent, timestamp, device["id"]),
                    )
                    self.app.cache_agent(token, device)
                    return device
        return None

    def require_agent(self) -> dict[str, Any]:
        token = self.agent_token()
        if not token:
            raise PermissionError("Agent token required")
        device = self.agent_from_token(token)
        if device is not None:
            return device
        raise PermissionError("Invalid agent token")

    def auth_error(self, exc: Exception, *, html_redirect: bool = False, next_path: str = "/") -> None:
        if html_redirect:
            self.send_redirect(f"/login?next={next_path}")
            return
        status = HTTPStatus.FORBIDDEN if "Admin role" in str(exc) else HTTPStatus.UNAUTHORIZED
        self.send_error_json(str(exc), status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                try:
                    self.require_user()
                except PermissionError as exc:
                    self.auth_error(exc, html_redirect=True, next_path="/")
                    return
                self.send_html(USER_HTML)
            elif parsed.path == "/admin":
                try:
                    self.require_admin()
                except PermissionError as exc:
                    self.auth_error(exc, html_redirect=True, next_path="/admin")
                    return
                self.send_html(ADMIN_HTML)
            elif parsed.path == "/login":
                self.send_html(LOGIN_HTML)
            elif parsed.path == "/agent-login":
                query = parse_qs(parsed.query)
                self.agent_login_redirect(query.get("token", [""])[0])
            elif parsed.path == "/api/bootstrap":
                user = self.require_user()
                self.send_json(self.api_bootstrap(user["id"]))
            elif parsed.path == "/api/route-lookup":
                user = self.require_user()
                query = parse_qs(parsed.query)
                lookup_user_id = user["id"]
                if user.get("role") == "admin" and query.get("user_id", [""])[0]:
                    lookup_user_id = query.get("user_id", [""])[0]
                self.send_json(route_lookup(self.app.db_path, self.app.inventory_path, user_id=lookup_user_id, target=query.get("target", [""])[0]))
            elif parsed.path == "/api/service-aliases":
                self.require_user()
                with self.app.conn() as conn:
                    self.send_json({"ok": True, "aliases": service_alias_rows(conn)})
            elif parsed.path == "/api/user/service-aliases":
                user = self.require_user()
                with self.app.conn() as conn:
                    self.send_json(
                        {
                            "ok": True,
                            "global": service_alias_rows(conn),
                            "local": user_service_alias_rows(conn, user_id=user["id"]),
                            "effective": effective_service_alias_rows(conn, user_id=user["id"]),
                        }
                    )
            elif parsed.path == "/api/critical-services":
                user = self.require_user()
                with self.app.conn() as conn:
                    self.send_json(
                        {
                            "ok": True,
                            "global": critical_service_rows(conn, user_id=""),
                            "local": critical_service_rows(conn, user_id=user["id"]),
                            "effective": effective_critical_services(conn, user_id=user["id"]),
                        }
                    )
            elif parsed.path == "/api/control/endpoints":
                self.send_json(control_endpoints_manifest())
            elif parsed.path == "/api/status":
                self.require_admin()
                self.send_json(build_system_status(self.app.db_path, self.app.inventory_path))
            elif parsed.path == "/api/admin":
                self.require_admin()
                self.send_json(self.api_admin())
            elif parsed.path == "/api/route-plan":
                self.require_admin()
                self.send_json(build_route_plan(self.app.db_path))
            elif parsed.path == "/api/admin/deploy-preview":
                self.require_admin()
                self.send_json(build_combined_deploy_preview(self.app.db_path, self.app.inventory_path))
            elif parsed.path == "/api/admin/auto-winners":
                self.require_admin()
                query = parse_qs(parsed.query)
                self.send_json(
                    recent_auto_winners(
                        self.app.db_path,
                        self.app.inventory_path,
                        target=query.get("target", [""])[0],
                        limit=int(query.get("limit", ["10"])[0] or "10"),
                    )
                )
            elif parsed.path == "/api/admin/domain-discovery":
                self.require_admin()
                query = parse_qs(parsed.query)
                with self.app.conn() as conn:
                    self.send_json(
                        {
                            "ok": True,
                            "items": domain_discovery_rows(
                                conn,
                                status=query.get("status", [""])[0],
                                limit=int(query.get("limit", ["100"])[0] or "100"),
                            ),
                        }
                    )
            elif parsed.path == "/api/agent/config":
                device = self.require_agent()
                with self.app.conn() as conn:
                    self.send_json(build_agent_config(conn, user_id=device["user_id"], device=device))
            elif parsed.path == "/api/agent/bootstrap":
                device = self.require_agent()
                self.send_json(self.api_bootstrap(device["user_id"]))
            elif parsed.path == "/api/agent/critical-services":
                device = self.require_agent()
                with self.app.conn() as conn:
                    self.send_json(
                        {
                            "ok": True,
                            "services": effective_critical_services(conn, user_id=device["user_id"]),
                        }
                    )
            elif parsed.path == "/api/agent/route-lookup":
                device = self.require_agent()
                query = parse_qs(parsed.query)
                self.send_json(
                    route_lookup(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=device["user_id"],
                        target=query.get("target", [""])[0],
                    )
                )
            elif parsed.path == "/api/agent/app-version":
                self.require_agent()
                query = parse_qs(parsed.query)
                self.send_json(agent_app_version_manifest(query.get("platform", ["android"])[0]))
            elif parsed.path == "/api/agent/update-package":
                self.require_agent()
                query = parse_qs(parsed.query)
                platform = normalize_platform(query.get("platform", [""])[0]) or "android"
                suffix = ".apk" if platform == "android" else ".zip"
                artifact = AGENT_UPDATE_DIR / f"{platform}{suffix}"
                if not artifact.exists() or not artifact.is_file():
                    self.send_error_json("Update package not found", HTTPStatus.NOT_FOUND)
                    return
                content_type = "application/vnd.android.package-archive" if suffix == ".apk" else "application/zip"
                self.send_binary_file(artifact, download_name=artifact.name, content_type=content_type)
            elif parsed.path == "/api/agent/probe-jobs":
                device = self.require_agent()
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["2"])[0] or "2")
                self.send_json({"ok": True, "jobs": claim_agent_probe_jobs(self.app.db_path, self.app.inventory_path, device=device, limit=limit)})
            elif parsed.path == "/api/admin/client-config":
                self.require_admin()
                query = parse_qs(parsed.query)
                user_id = query.get("user_id", [""])[0]
                config_path = find_cudy_client_config(user_id)
                if not config_path:
                    self.send_error_json("Client config not found", HTTPStatus.NOT_FOUND)
                    return
                self.send_file(config_path, download_name=config_path.name)
            elif parsed.path == "/healthz":
                self.send_json({"ok": True})
            elif parsed.path == "/readyz":
                readiness = build_readiness_status(self.app.db_path, self.app.inventory_path)
                status = HTTPStatus.OK if readiness.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
                self.send_json(readiness, status)
            else:
                self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except PermissionError as exc:
            self.auth_error(exc)
        except Exception as exc:
            self.send_error_json(str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/login":
                self.api_login(data)
            elif parsed.path == "/api/logout":
                self.api_logout()
            elif parsed.path == "/api/user/default-server":
                self.require_user()
                self.send_json(self.api_set_default_server(data))
            elif parsed.path == "/api/domain-routes":
                self.require_user()
                self.send_json(self.api_save_domain_route(data))
            elif parsed.path == "/api/service-aliases":
                self.require_admin()
                self.send_json(
                    save_service_alias(
                        self.app.db_path,
                        self.app.inventory_path,
                        alias=str(data.get("alias") or ""),
                        label=str(data.get("label") or ""),
                        targets=data.get("targets") or "",
                    )
                )
            elif parsed.path == "/api/user/service-aliases":
                user = self.require_user()
                self.send_json(
                    save_user_service_alias(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=user["id"],
                        alias=str(data.get("alias") or ""),
                        label=str(data.get("label") or ""),
                        targets=data.get("targets") or "",
                    )
                )
            elif parsed.path == "/api/critical-services":
                user = self.require_user()
                self.send_json(
                    save_critical_service(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=user["id"],
                        service_key=str(data.get("service_key") or data.get("label") or ""),
                        label=str(data.get("label") or ""),
                        targets=data.get("targets") or "",
                        success_pattern=str(data.get("success_pattern") or ""),
                        failure_pattern=str(data.get("failure_pattern") or ""),
                        routing_enabled=data.get("routing_enabled") is True,
                        candidate_server_ids=data.get("candidate_server_ids") or [],
                        enabled=data.get("enabled") is not False,
                    )
                )
            elif parsed.path == "/api/admin/servers":
                self.require_admin()
                self.send_json(self.api_update_server(data))
            elif parsed.path == "/api/admin/users":
                self.require_admin()
                self.send_json(self.api_admin_save_user(data))
            elif parsed.path == "/api/admin/user-password":
                self.require_admin()
                self.send_json(self.api_admin_set_password(data))
            elif parsed.path == "/api/admin/user-default-server":
                self.require_admin()
                self.send_json(self.api_admin_set_default_server(data))
            elif parsed.path == "/api/admin/domain-routes":
                self.require_admin()
                self.send_json(self.api_admin_save_domain_route(data))
            elif parsed.path == "/api/admin/global-domain-routes":
                self.require_admin()
                self.send_json(self.api_admin_save_global_domain_route(data))
            elif parsed.path == "/api/admin/auto-cache":
                self.require_admin()
                self.send_json(self.api_admin_save_auto_cache(data))
            elif parsed.path == "/api/admin/auto-candidates":
                self.require_admin()
                self.send_json(self.api_admin_save_auto_candidates(data))
            elif parsed.path == "/api/admin/domain-discovery":
                self.require_admin()
                self.send_json(
                    save_domain_discovery_status(
                        self.app.db_path,
                        self.app.inventory_path,
                        domain=str(data.get("domain") or ""),
                        status=str(data.get("status") or ""),
                        note=str(data.get("note") or ""),
                    )
                )
            elif parsed.path == "/api/admin/critical-services":
                self.require_admin()
                self.send_json(
                    save_critical_service(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=str(data.get("user_id") or ""),
                        service_key=str(data.get("service_key") or data.get("label") or ""),
                        label=str(data.get("label") or ""),
                        targets=data.get("targets") or "",
                        success_pattern=str(data.get("success_pattern") or ""),
                        failure_pattern=str(data.get("failure_pattern") or ""),
                        routing_enabled=data.get("routing_enabled") is True,
                        candidate_server_ids=data.get("candidate_server_ids") or [],
                        enabled=data.get("enabled") is not False,
                    )
                )
            elif parsed.path == "/api/admin/domain-discovery/promote":
                self.require_admin()
                self.send_json(
                    promote_domain_discovery_to_auto_route(
                        self.app.db_path,
                        self.app.inventory_path,
                        domain=str(data.get("domain") or ""),
                        user_id=str(data.get("user_id") or ""),
                        candidate_server_ids=data.get("candidate_server_ids"),
                        note=str(data.get("note") or ""),
                        probe_now=bool(data.get("probe_now")),
                        max_probe_candidates=max(1, min(int(data.get("max_probe_candidates") or 4), 50)),
                    )
                )
            elif parsed.path == "/api/admin/auto-select":
                self.require_admin()
                self.send_json(self.api_admin_auto_select(data))
            elif parsed.path == "/api/admin/auto-worker-once":
                self.require_admin()
                self.send_json(self.api_admin_auto_worker_once(data))
            elif parsed.path == "/api/admin/provider-refresh":
                self.require_admin()
                self.send_json(self.api_admin_provider_refresh(data))
            elif parsed.path == "/api/admin/deploy-routes":
                self.require_admin()
                self.send_json(self.api_admin_deploy_routes(data))
            elif parsed.path == "/api/admin/sync-cudy-clients":
                self.require_admin()
                self.send_json(sync_cudy_clients_from_router(self.app.db_path, self.app.inventory_path))
            elif parsed.path == "/api/admin/enrollment-codes":
                self.require_admin()
                self.send_json(self.api_admin_create_enrollment_code(data))
            elif parsed.path == "/api/admin/agent-devices":
                self.require_admin()
                device_id = str(data.get("id") or "")
                result = set_agent_device_enabled(
                    self.app.db_path,
                    self.app.inventory_path,
                    device_id=device_id,
                    enabled=bool(data.get("enabled")),
                )
                result["cached_tokens_invalidated"] = self.app.invalidate_agent(
                    device_id,
                    deny_cached=not bool(data.get("enabled")),
                )
                self.send_json(result)
            elif parsed.path == "/api/agent/enroll":
                self.send_json(self.api_agent_enroll(data))
            elif parsed.path == "/api/agent/status":
                device = self.require_agent()
                self.send_json(self.api_agent_status(device, data))
            elif parsed.path == "/api/agent/diagnostics":
                device = self.require_agent()
                self.send_json(
                    save_agent_diagnostic_report(
                        self.app.db_path,
                        self.app.inventory_path,
                        device=device,
                        summary=str(data.get("summary") or ""),
                        report_text=str(data.get("report") or ""),
                    )
                )
            elif parsed.path == "/api/agent/user-default-server":
                device = self.require_agent()
                self.send_json(self.api_agent_set_default_server(device, data))
            elif parsed.path == "/api/agent/domain-routes":
                device = self.require_agent()
                self.send_json(self.api_agent_save_domain_route(device, data))
            elif parsed.path == "/api/agent/probe-jobs/result":
                device = self.require_agent()
                self.send_json(
                    complete_agent_probe_job(
                        self.app.db_path,
                        self.app.inventory_path,
                        device=device,
                        job_id=str(data.get("job_id") or ""),
                        result=data.get("result") or {},
                    )
                )
            else:
                self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except PermissionError as exc:
            self.auth_error(exc)
        except Exception as exc:
            self.send_error_json(str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/domain-routes":
                user = self.require_user()
                query = parse_qs(parsed.query)
                domain = normalize_domain(query.get("domain", [""])[0])
                with self.app.conn() as conn:
                    conn.execute("DELETE FROM user_domain_routes WHERE user_id = ? AND domain = ?", (user["id"], domain))
                self.send_json({"ok": True})
            elif parsed.path == "/api/admin/domain-routes":
                self.require_admin()
                query = parse_qs(parsed.query)
                user_id = str(query.get("user_id", [""])[0])
                domain = normalize_domain(query.get("domain", [""])[0])
                with self.app.conn() as conn:
                    conn.execute("DELETE FROM user_domain_routes WHERE user_id = ? AND domain = ?", (user_id, domain))
                self.send_json({"ok": True})
            elif parsed.path == "/api/admin/global-domain-routes":
                self.require_admin()
                query = parse_qs(parsed.query)
                domain = normalize_domain(query.get("domain", [""])[0])
                with self.app.conn() as conn:
                    conn.execute("DELETE FROM global_domain_routes WHERE domain = ?", (domain,))
                self.send_json({"ok": True})
            elif parsed.path == "/api/admin/users":
                self.require_admin()
                query = parse_qs(parsed.query)
                self.send_json(
                    delete_admin_user(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=query.get("id", [""])[0],
                        revoke_cudy=query.get("revoke_cudy", ["0"])[0] in {"1", "true", "yes"},
                    )
                )
            elif parsed.path == "/api/admin/auto-cache":
                self.require_admin()
                query = parse_qs(parsed.query)
                domain = query.get("domain", [""])[0]
                self.send_json(delete_auto_cache_entry(self.app.db_path, self.app.inventory_path, domain))
            elif parsed.path == "/api/admin/auto-candidates":
                self.require_admin()
                query = parse_qs(parsed.query)
                self.send_json(
                    delete_auto_candidate_policy(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=query.get("user_id", [""])[0],
                        domain=query.get("domain", [""])[0],
                    )
                )
            elif parsed.path == "/api/admin/enrollment-codes":
                self.require_admin()
                query = parse_qs(parsed.query)
                self.send_json(
                    revoke_agent_enrollment_code(
                        self.app.db_path,
                        self.app.inventory_path,
                        code_id=query.get("id", [""])[0],
                    )
                )
            elif parsed.path == "/api/admin/agent-devices":
                self.require_admin()
                query = parse_qs(parsed.query)
                device_id = query.get("id", [""])[0]
                if query.get("hard", ["0"])[0] in {"1", "true", "yes"}:
                    result = delete_agent_device(
                        self.app.db_path,
                        self.app.inventory_path,
                        device_id=device_id,
                    )
                else:
                    result = revoke_agent_device(
                        self.app.db_path,
                        self.app.inventory_path,
                        device_id=device_id,
                    )
                result["cached_tokens_invalidated"] = self.app.invalidate_agent(device_id, deny_cached=True)
                self.send_json(result)
            elif parsed.path == "/api/service-aliases":
                self.require_admin()
                query = parse_qs(parsed.query)
                self.send_json(delete_service_alias(self.app.db_path, self.app.inventory_path, alias=query.get("alias", [""])[0]))
            elif parsed.path == "/api/user/service-aliases":
                user = self.require_user()
                query = parse_qs(parsed.query)
                self.send_json(
                    delete_user_service_alias(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=user["id"],
                        alias=query.get("alias", [""])[0],
                    )
                )
            elif parsed.path == "/api/critical-services":
                user = self.require_user()
                query = parse_qs(parsed.query)
                self.send_json(
                    delete_critical_service(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=user["id"],
                        service_key=query.get("service_key", [""])[0],
                    )
                )
            elif parsed.path == "/api/admin/critical-services":
                self.require_admin()
                query = parse_qs(parsed.query)
                self.send_json(
                    delete_critical_service(
                        self.app.db_path,
                        self.app.inventory_path,
                        user_id=query.get("user_id", [""])[0],
                        service_key=query.get("service_key", [""])[0],
                    )
                )
            else:
                self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self.auth_error(exc)
        except Exception as exc:
            self.send_error_json(str(exc))

    def api_login(self, data: dict[str, Any]) -> None:
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not username or not password:
            raise ValueError("User and password are required")
        with self.app.conn() as conn:
            user = row(
                conn,
                """
                SELECT id, display_name, role, password_salt, password_hash, enabled
                FROM users
                WHERE id = ?
                """,
                (username,),
            )
            if user is None or not user.get("enabled"):
                raise PermissionError("Invalid user or password")
            if not verify_password(password, user.get("password_salt"), user.get("password_hash")):
                raise PermissionError("Invalid user or password")
            token = secrets.token_urlsafe(32)
            expires_at = int(time.time()) + SESSION_TTL_SECONDS
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user["id"], now(), expires_at),
            )
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}"
        )
        self.send_json(
            {"ok": True, "user": {"id": user["id"], "display_name": user["display_name"], "role": user["role"]}},
            extra_headers=[("set-cookie", cookie)],
        )

    def api_logout(self) -> None:
        token = self.cookie_value(SESSION_COOKIE)
        if token:
            with self.app.conn() as conn:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        expired = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        self.send_json({"ok": True}, extra_headers=[("set-cookie", expired)])

    def agent_login_redirect(self, token: str) -> None:
        device = self.agent_from_token(str(token or "").strip())
        if device is None:
            raise PermissionError("Invalid agent token")
        session_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        with self.app.conn() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (session_token, device["user_id"], now(), expires_at),
            )
        cookie = (
            f"{SESSION_COOKIE}={session_token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}"
        )
        self.send_redirect("/", extra_headers=[("set-cookie", cookie)])

    def api_agent_status(self, device: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now()
        payload = {
            "schema_version": int(data.get("schema_version") or 1),
            "platform": str(data.get("platform") or device.get("platform") or ""),
            "agent_version": str(data.get("agent_version") or ""),
            "vpn_interfaces": data.get("vpn_interfaces") or [],
            "routes": data.get("routes") or [],
            "domain_routes": data.get("domain_routes") or [],
            "ip_routes": data.get("ip_routes") or [],
            "dns": data.get("dns") or {},
            "health": data.get("health") or {},
            "capabilities": data.get("capabilities") or {},
            "errors": data.get("errors") or [],
            "raw": data.get("raw") or {},
        }
        with self.app.conn() as conn:
            conn.execute(
                """
                INSERT INTO agent_status (device_id, status_json, reported_at)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id)
                DO UPDATE SET status_json = excluded.status_json, reported_at = excluded.reported_at
                """,
                (device["id"], json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp),
            )
            conn.execute(
                "UPDATE agent_devices SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, device["id"]),
            )
        return {"ok": True, "device_id": device["id"], "reported_at": timestamp}

    def api_bootstrap(self, user_id: str) -> dict[str, Any]:
        with self.app.conn() as conn:
            user = row(
                conn,
                "SELECT id, display_name, role, default_server_id, client_ip, enabled FROM users WHERE id = ?",
                (user_id,),
            )
            if user is None:
                raise ValueError(f"Unknown user: {user_id}")
            routes = rows(
                conn,
                """
                SELECT domain, server_id, enabled, updated_at
                FROM user_domain_routes
                WHERE user_id = ?
                ORDER BY domain
                """,
                (user_id,),
            )
            for route_item in routes:
                route_item["auto_candidate_policy"] = resolve_auto_candidate_policy(
                    conn,
                    user_id=user_id,
                    domain=route_item["domain"],
                )
            default_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain="")
            return {
                "user": user,
                "servers": user_servers(conn),
                "routes": routes,
                "aliases": effective_service_alias_rows(conn, user_id=user_id),
                "critical_services": {
                    "global": critical_service_rows(conn, user_id=""),
                    "local": critical_service_rows(conn, user_id=user_id),
                    "effective": effective_critical_services(conn, user_id=user_id),
                },
                "default_auto_candidate_policy": default_policy,
            }

    def api_agent_enroll(self, data: dict[str, Any]) -> dict[str, Any]:
        return consume_agent_enrollment_code(
            self.app.db_path,
            self.app.inventory_path,
            code=str(data.get("code") or ""),
            device_id=str(data.get("device_id") or "") or None,
            display_name=str(data.get("display_name") or "") or None,
            platform=str(data.get("platform") or "android"),
        )

    def api_admin_create_enrollment_code(self, data: dict[str, Any]) -> dict[str, Any]:
        return create_agent_enrollment_code(
            self.app.db_path,
            self.app.inventory_path,
            user_id=str(data.get("user_id") or ""),
            device_id=str(data.get("device_id") or "") or None,
            display_name=str(data.get("display_name") or "") or None,
            platform=str(data.get("platform") or "android"),
            ttl_hours=int(data.get("ttl_hours") or 24),
            enabled=True,
        )

    def api_admin(self) -> dict[str, Any]:
        with self.app.conn() as conn:
            agent_status = rows(
                conn,
                """
                SELECT d.id AS device_id, d.user_id, d.display_name, d.platform,
                       d.enabled, d.last_seen_at, d.last_ip, s.reported_at, s.status_json
                FROM agent_devices d
                LEFT JOIN agent_status s ON s.device_id = d.id
                ORDER BY d.user_id, d.id
                """,
            )
            for item in agent_status:
                try:
                    item["status"] = json.loads(item.pop("status_json") or "{}")
                except json.JSONDecodeError:
                    item["status"] = {}
            return {
                "servers": admin_servers(conn),
                "users": rows(
                    conn,
                    """
                    SELECT id, display_name, role, default_server_id, client_ip, enabled,
                           CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END AS has_login,
                           updated_at
                    FROM users
                    ORDER BY id
                    """,
                ),
                "routes": rows(
                    conn,
                    """
                    SELECT user_id, domain, server_id, enabled, updated_at
                    FROM user_domain_routes
                    ORDER BY user_id, domain
                    """,
                ),
                "global_routes": rows(
                    conn,
                    """
                    SELECT domain, server_id, enabled, updated_at
                    FROM global_domain_routes
                    ORDER BY domain
                    """,
                ),
                "global_ip_routes": rows(
                    conn,
                    """
                    SELECT target_cidr, server_id, enabled, source, note, updated_at
                    FROM global_ip_routes
                    ORDER BY server_id, target_cidr
                    """,
                ),
                "auto_cache": rows(
                    conn,
                    "SELECT domain, selected_server_id, score_ms, status, checked_at FROM domain_auto_cache ORDER BY domain",
                ),
                "auto_candidates": auto_candidate_policy_rows(conn),
                "transport_configs": transport_config_summaries(conn),
                "service_aliases": service_alias_rows(conn),
                "critical_services": critical_service_rows(conn),
                "domain_discovery": domain_discovery_rows(conn, limit=100),
                "probe_jobs": [
                    probe_job_row_to_dict(item)
                    for item in rows(
                        conn,
                        """
                        SELECT *
                        FROM agent_probe_jobs
                        ORDER BY created_at DESC
                        LIMIT 50
                        """,
                    )
                ],
                "agent_devices": rows(
                    conn,
                    """
                    SELECT d.id, d.user_id, u.display_name AS user_display_name,
                           d.display_name, d.platform, d.enabled, d.last_seen_at,
                           d.last_ip, d.last_user_agent, d.created_at, d.updated_at,
                           s.reported_at AS status_reported_at
                    FROM agent_devices d
                    JOIN users u ON u.id = d.user_id
                    LEFT JOIN agent_status s ON s.device_id = d.id
                    ORDER BY d.user_id, d.id
                    """,
                ),
                "agent_diagnostics": agent_diagnostic_rows(self.app.db_path, self.app.inventory_path, limit=20),
                "agent_enrollment_codes": list_agent_enrollment_codes(self.app.db_path, self.app.inventory_path),
                "agent_updates": [
                    agent_app_version_manifest(platform)
                    for platform in ("android", "windows", "linux")
                ],
                "agent_status": agent_status,
            }

    def api_set_default_server(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = self.require_user()["id"]
        return self.save_default_server_for_user(user_id, data)

    def api_agent_set_default_server(self, device: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        return self.save_default_server_for_user(str(device["user_id"]), data)

    def save_default_server_for_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        server_id = str(data.get("server_id") or "")
        candidate_server_ids = data.get("auto_candidate_server_ids")
        timestamp = now()
        with self.app.conn() as conn:
            validate_server_id(conn, server_id, require_user_visible=True)
            cursor = conn.execute(
                "UPDATE users SET default_server_id = ?, updated_at = ? WHERE id = ?",
                (server_id, timestamp, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown user: {user_id}")
        auto_candidate_policy = sync_route_auto_candidate_policy(
            self.app.db_path,
            self.app.inventory_path,
            user_id=user_id,
            domain="",
            server_id=server_id,
            candidate_server_ids=candidate_server_ids,
        )
        return {"ok": True, "auto_candidate_policy": auto_candidate_policy}

    def api_admin_save_user(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = str(data.get("id") or "").strip()
        display_name = str(data.get("display_name") or user_id).strip()
        role = str(data.get("role") or "user")
        default_server_id = str(data.get("default_server_id") or "auto")
        client_ip = normalize_client_ip(str(data.get("client_ip") or ""))
        enabled = int(bool(data.get("enabled")))
        create_cudy_client = bool(data.get("create_cudy_client"))
        password_raw = data.get("password")
        password = None if password_raw in (None, "") else str(password_raw)
        if password is not None:
            if len(password) < 8:
                raise ValueError("Password must be at least 8 characters")
        if role not in {"admin", "user"}:
            raise ValueError("role must be admin or user")
        if not user_id or not re.match(r"^[A-Za-z0-9_.-]{2,64}$", user_id):
            raise ValueError("user id must be 2-64 chars: A-Z a-z 0-9 _ . -")
        if not display_name:
            raise ValueError("display name is required")
        if create_cudy_client and role != "user":
            raise ValueError("Cudy VPN client creation is only supported for role=user")
        timestamp = now()
        cudy_client: dict[str, Any] | None = None
        with self.app.conn() as conn:
            validate_server_id(conn, default_server_id, require_user_visible=True)
            existing = row(conn, "SELECT id FROM users WHERE id = ?", (user_id,))
        if existing is None and create_cudy_client:
            cudy_client = create_cudy_vpn_client(client_name=user_id)
            if cudy_client.get("client_ip"):
                client_ip = normalize_client_ip(str(cudy_client["client_ip"]))
        if existing is None:
            if not password and not client_ip:
                raise ValueError("Password or client_ip is required for a new user")
            with self.app.conn() as conn:
                if password:
                    salt, password_hash = hash_password(password)
                else:
                    salt, password_hash = None, None
                conn.execute(
                    """
                    INSERT INTO users (
                      id, display_name, role, default_server_id, client_ip, password_salt,
                      password_hash, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        display_name,
                        role,
                        default_server_id,
                        client_ip,
                        salt,
                        password_hash,
                        enabled,
                        timestamp,
                        timestamp,
                    ),
                )
        else:
            if create_cudy_client:
                raise ValueError("Cudy VPN client creation is only supported for new users")
            with self.app.conn() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, default_server_id = ?, client_ip = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, default_server_id, client_ip, enabled, timestamp, user_id),
                )
                if password:
                    salt, password_hash = hash_password(password)
                    conn.execute(
                        """
                        UPDATE users
                        SET password_salt = ?, password_hash = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (salt, password_hash, timestamp, user_id),
                    )
        result = {"ok": True}
        if cudy_client:
            result["cudy_client"] = cudy_client
        return result

    def api_admin_set_password(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = str(data.get("id") or "").strip()
        password = str(data.get("password") or "")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        salt, password_hash = hash_password(password)
        with self.app.conn() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_salt = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                (salt, password_hash, now(), user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown user: {user_id}")
        return {"ok": True}

    def api_admin_set_default_server(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = str(data.get("id") or "").strip()
        server_id = str(data.get("server_id") or "")
        with self.app.conn() as conn:
            validate_server_id(conn, server_id, require_user_visible=True)
            cursor = conn.execute(
                "UPDATE users SET default_server_id = ?, updated_at = ? WHERE id = ?",
                (server_id, now(), user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown user: {user_id}")
        return {"ok": True}

    def api_admin_save_domain_route(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = str(data.get("user_id") or "").strip()
        domain = normalize_domain(str(data.get("domain") or ""))
        server_id = str(data.get("server_id") or "")
        candidate_server_ids = data.get("auto_candidate_server_ids")
        timestamp = now()
        with self.app.conn() as conn:
            validate_server_id(conn, server_id, require_user_visible=True)
            if row(conn, "SELECT id FROM users WHERE id = ?", (user_id,)) is None:
                raise ValueError(f"Unknown user: {user_id}")
            conn.execute(
                """
                INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, domain)
                DO UPDATE SET server_id = excluded.server_id, enabled = 1, updated_at = excluded.updated_at
                """,
                (user_id, domain, server_id, timestamp, timestamp),
            )
        auto_candidate_policy = sync_route_auto_candidate_policy(
            self.app.db_path,
            self.app.inventory_path,
            user_id=user_id,
            domain=domain,
            server_id=server_id,
            candidate_server_ids=candidate_server_ids,
        )
        return {
            "ok": True,
            "user_id": user_id,
            "domain": domain,
            "server_id": server_id,
            "auto_candidate_policy": auto_candidate_policy,
        }

    def api_admin_save_global_domain_route(self, data: dict[str, Any]) -> dict[str, Any]:
        domain = normalize_domain(str(data.get("domain") or ""))
        server_id = str(data.get("server_id") or "")
        candidate_server_ids = data.get("auto_candidate_server_ids")
        timestamp = now()
        with self.app.conn() as conn:
            validate_server_id(conn, server_id, require_user_visible=True)
            conn.execute(
                """
                INSERT INTO global_domain_routes (domain, server_id, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(domain)
                DO UPDATE SET server_id = excluded.server_id, enabled = 1, updated_at = excluded.updated_at
                """,
                (domain, server_id, timestamp, timestamp),
            )
        auto_candidate_policy = sync_route_auto_candidate_policy(
            self.app.db_path,
            self.app.inventory_path,
            user_id="",
            domain=domain,
            server_id=server_id,
            candidate_server_ids=candidate_server_ids,
        )
        return {
            "ok": True,
            "domain": domain,
            "server_id": server_id,
            "auto_candidate_policy": auto_candidate_policy,
        }

    def api_admin_save_auto_cache(self, data: dict[str, Any]) -> dict[str, Any]:
        score_value = data.get("score_ms")
        score_ms = None if score_value in (None, "") else int(score_value)
        return save_auto_cache_entry(
            self.app.db_path,
            self.app.inventory_path,
            domain=str(data.get("domain") or ""),
            selected_server_id=str(data.get("selected_server_id") or ""),
            score_ms=score_ms,
            status=str(data.get("status") or "manual"),
        )

    def api_admin_save_auto_candidates(self, data: dict[str, Any]) -> dict[str, Any]:
        return save_auto_candidate_policy(
            self.app.db_path,
            self.app.inventory_path,
            user_id=str(data.get("user_id") or ""),
            domain=str(data.get("domain") or ""),
            candidate_server_ids=data.get("candidate_server_ids") or "",
            enabled=bool(data.get("enabled", True)),
        )

    def api_admin_auto_select(self, data: dict[str, Any]) -> dict[str, Any]:
        return auto_select_domain(
            self.app.db_path,
            self.app.inventory_path,
            domain=str(data.get("domain") or ""),
            user_id=str(data.get("user_id") or ""),
            candidate_server_ids=data.get("candidate_server_ids") or None,
            url=str(data.get("url") or "") or None,
            apply=bool(data.get("apply", True)),
            deploy=bool(data.get("deploy", False)),
            switch_profiles=bool(data.get("switch_profiles", False)),
        )

    def api_admin_auto_worker_once(self, data: dict[str, Any]) -> dict[str, Any]:
        return create_auto_probe_jobs_once(
            self.app.db_path,
            self.app.inventory_path,
            cache_ttl_seconds=max(0, min(int(data.get("cache_ttl_seconds") or 3600), 30 * 24 * 3600)),
            job_stale_seconds=max(60, min(int(data.get("job_stale_seconds") or 900), 24 * 3600)),
            agent_stale_seconds=max(60, min(int(data.get("agent_stale_seconds") or 600), 24 * 3600)),
            max_jobs=max(1, min(int(data.get("max_jobs") or 5), 50)),
            max_candidates_per_job=max(1, min(int(data.get("max_candidates_per_job") or 4), 50)),
            connect_timeout=max(1, min(int(data.get("connect_timeout") or 5), 60)),
            max_time=max(1, min(int(data.get("max_time") or 12), 120)),
            active_domain_limit=max(1, min(int(data.get("active_domain_limit") or 300), 5000)),
        )

    def api_admin_provider_refresh(self, data: dict[str, Any]) -> dict[str, Any]:
        provider = str(data.get("provider") or "all")
        if provider not in {"vpntype", "lokvpn", "all"}:
            raise ValueError("provider must be vpntype, lokvpn, or all")
        servers_raw = data.get("servers") or ""
        server_ids = parse_candidate_server_ids(servers_raw) if servers_raw else None
        return refresh_provider_transports(
            self.app.db_path,
            self.app.inventory_path,
            provider=provider,
            server_ids=server_ids,
            skip_verify=bool(data.get("skip_verify")),
            connect_timeout=max(1, min(int(data.get("connect_timeout") or 5), 60)),
            max_time=max(1, min(int(data.get("max_time") or 12), 120)),
        )

    def api_admin_deploy_routes(self, data: dict[str, Any]) -> dict[str, Any]:
        password = load_cudy_ssh_password()
        if not password:
            raise ValueError(
                "Cudy SSH password is not configured. Set CUDY_SSH_PASSWORD or create secrets/cudy_ssh_password.txt"
            )
        return apply_combined_route_deploy(
            self.app.db_path,
            self.app.inventory_path,
            ssh_password=password,
            install_scripts=bool(data.get("install_scripts")),
            restart_pbr=not bool(data.get("no_restart_pbr")),
            run_user_apply=not bool(data.get("no_run_user_apply")),
            prune_empty=bool(data.get("prune_empty")),
            allow_empty=bool(data.get("allow_empty")),
        )

    def api_save_domain_route(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = self.require_user()["id"]
        return self.save_domain_route_for_user(user_id, data)

    def api_agent_save_domain_route(self, device: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        return self.save_domain_route_for_user(str(device["user_id"]), data)

    def save_domain_route_for_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        domain = normalize_domain(str(data.get("domain") or ""))
        server_id = str(data.get("server_id") or "")
        candidate_server_ids = data.get("auto_candidate_server_ids")
        timestamp = now()
        with self.app.conn() as conn:
            validate_server_id(conn, server_id, require_user_visible=True)
            if row(conn, "SELECT id FROM users WHERE id = ?", (user_id,)) is None:
                raise ValueError(f"Unknown user: {user_id}")
            conn.execute(
                """
                INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, domain)
                DO UPDATE SET server_id = excluded.server_id, enabled = 1, updated_at = excluded.updated_at
                """,
                (user_id, domain, server_id, timestamp, timestamp),
            )
        auto_candidate_policy = sync_route_auto_candidate_policy(
            self.app.db_path,
            self.app.inventory_path,
            user_id=user_id,
            domain=domain,
            server_id=server_id,
            candidate_server_ids=candidate_server_ids,
        )
        return {
            "ok": True,
            "domain": domain,
            "server_id": server_id,
            "auto_candidate_policy": auto_candidate_policy,
        }

    def api_update_server(self, data: dict[str, Any]) -> dict[str, Any]:
        server_id = str(data.get("id") or "")
        label = str(data.get("label") or "").strip()
        if not server_id:
            raise ValueError("Server id is required")
        if not label:
            raise ValueError("Label is required")
        timestamp = now()
        with self.app.conn() as conn:
            if row(conn, "SELECT id FROM servers WHERE id = ?", (server_id,)) is None:
                raise ValueError(f"Unknown server: {server_id}")
            conn.execute(
                """
                UPDATE servers
                SET label = ?, enabled = ?, user_visible = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    label,
                    int(bool(data.get("enabled"))),
                    int(bool(data.get("user_visible"))),
                    timestamp,
                    server_id,
                ),
            )
        return {"ok": True}


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: App):
        super().__init__(address, Handler)
        self.app = app


def print_db_summary(db_path: Path) -> None:
    with connect(db_path) as conn:
        server_count = conn.execute("SELECT count(*) FROM servers").fetchone()[0]
        user_server_count = conn.execute(
            "SELECT count(*) FROM servers WHERE enabled = 1 AND user_visible = 1"
        ).fetchone()[0]
        user_count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        login_user_count = conn.execute("SELECT count(*) FROM users WHERE password_hash IS NOT NULL").fetchone()[0]
        route_count = conn.execute("SELECT count(*) FROM user_domain_routes").fetchone()[0]
        ip_route_count = conn.execute("SELECT count(*) FROM user_ip_routes").fetchone()[0]
        global_ip_route_count = conn.execute("SELECT count(*) FROM global_ip_routes").fetchone()[0]
        auto_cache_count = conn.execute("SELECT count(*) FROM domain_auto_cache").fetchone()[0]
        auto_candidate_count = conn.execute("SELECT count(*) FROM auto_candidate_policies").fetchone()[0]
        agent_device_count = conn.execute("SELECT count(*) FROM agent_devices").fetchone()[0]
    print(f"DB: {db_path}")
    print(f"Servers: {server_count} total, {user_server_count} user-visible")
    print(f"Users: {user_count} total, {login_user_count} with login")
    print(f"Domain routes: {route_count}")
    print(f"IP/CIDR routes: {ip_route_count}")
    print(f"Global IP/CIDR routes: {global_ip_route_count}")
    print(f"Auto cache: {auto_cache_count}")
    print(f"Auto priority policies: {auto_candidate_count}")
    print(f"Agent devices: {agent_device_count}")


def read_password_arg(value: str | None, *, confirm: bool) -> str:
    if value is not None:
        return value
    first = getpass.getpass("Password: ")
    if confirm:
        second = getpass.getpass("Confirm password: ")
        if first != second:
            raise ValueError("Passwords do not match")
    return first


def read_json_arg(value: str) -> dict[str, Any]:
    raw = value.strip()
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON value must be an object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local VPN control web app.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init-db", help="Initialize or update the SQLite database.")
    init_parser.add_argument("--reset-from-inventory", action="store_true")

    summary_parser = sub.add_parser("summary", help="Print database summary.")

    control_endpoints_parser = sub.add_parser("control-endpoints", help="Print advertised primary/fallback control endpoints.")
    control_endpoints_parser.add_argument("--json", action="store_true", help="Print JSON.")

    system_status_parser = sub.add_parser("system-status", help="Print production health/status summary.")
    system_status_parser.add_argument("--json", action="store_true", help="Print JSON.")
    system_status_parser.add_argument("--strict", action="store_true", help="Exit with a non-zero status when production status is degraded.")
    status_parser = sub.add_parser("status", help="Alias for system-status.")
    status_parser.add_argument("--json", action="store_true", help="Print JSON.")
    status_parser.add_argument("--strict", action="store_true", help="Exit with a non-zero status when production status is degraded.")

    create_user_parser = sub.add_parser("create-user", help="Create or update a login user.")
    create_user_parser.add_argument("user_id")
    create_user_parser.add_argument("--display-name")
    create_user_parser.add_argument("--role", choices=["admin", "user"], default="user")
    create_user_parser.add_argument("--client-ip")
    create_user_parser.add_argument("--password", help="Prefer interactive prompt or env in normal use.")
    create_user_parser.add_argument("--disabled", action="store_true")
    create_user_parser.add_argument("--no-password-change", action="store_true")

    service_user_parser = sub.add_parser("service-user-create", help="Create or update a non-login service user for agents.")
    service_user_parser.add_argument("user_id")
    service_user_parser.add_argument("--display-name")
    service_user_parser.add_argument("--client-ip")
    service_user_parser.add_argument("--disabled", action="store_true")
    service_user_parser.add_argument("--json", action="store_true", help="Print JSON.")

    device_create_parser = sub.add_parser("device-create", help="Create or rotate an agent device token.")
    device_create_parser.add_argument("user_id")
    device_create_parser.add_argument("--device-id")
    device_create_parser.add_argument("--display-name")
    device_create_parser.add_argument("--platform", choices=["linux", "windows", "android", "macos", "other"], default="other")
    device_create_parser.add_argument("--disabled", action="store_true")
    device_create_parser.add_argument("--json", action="store_true", help="Print JSON, including the one-time token.")

    device_list_parser = sub.add_parser("device-list", help="List agent devices.")
    device_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    device_revoke_parser = sub.add_parser("device-revoke", help="Disable an agent device token.")
    device_revoke_parser.add_argument("device_id")
    device_revoke_parser.add_argument("--json", action="store_true", help="Print JSON.")

    device_status_parser = sub.add_parser("device-status", help="List last reported agent status.")
    device_status_parser.add_argument("--json", action="store_true", help="Print JSON.")

    enrollment_create_parser = sub.add_parser("enrollment-create", help="Create a one-time Android/agent enrollment code.")
    enrollment_create_parser.add_argument("user_id")
    enrollment_create_parser.add_argument("--device-id")
    enrollment_create_parser.add_argument("--display-name")
    enrollment_create_parser.add_argument("--platform", choices=["linux", "windows", "android", "macos", "other"], default="android")
    enrollment_create_parser.add_argument("--ttl-hours", type=int, default=24)
    enrollment_create_parser.add_argument("--disabled", action="store_true")
    enrollment_create_parser.add_argument("--json", action="store_true", help="Print JSON, including the one-time code.")

    enrollment_list_parser = sub.add_parser("enrollment-list", help="List one-time enrollment codes without secret values.")
    enrollment_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    enrollment_revoke_parser = sub.add_parser("enrollment-revoke", help="Disable a one-time enrollment code.")
    enrollment_revoke_parser.add_argument("code_id")
    enrollment_revoke_parser.add_argument("--json", action="store_true", help="Print JSON.")

    transport_list_parser = sub.add_parser("transport-list", help="List control-server transport configs.")
    transport_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    transport_http_parser = sub.add_parser("transport-set-http", help="Set HTTP proxy TUN config for an agent transport.")
    transport_http_parser.add_argument("server_id")
    transport_http_parser.add_argument("--proxy-host", required=True)
    transport_http_parser.add_argument("--proxy-port", required=True, type=int)
    transport_http_parser.add_argument("--proxy-type", choices=["http", "socks"], default="http")
    transport_http_parser.add_argument("--interface-name")
    transport_http_parser.add_argument("--source", default="")
    transport_http_parser.add_argument("--version", default="")
    transport_http_parser.add_argument("--expires-at")
    transport_http_parser.add_argument("--disabled", action="store_true")
    transport_http_parser.add_argument("--json", action="store_true", help="Print JSON.")

    transport_json_parser = sub.add_parser("transport-set-json", help="Set a generic JSON config for an agent transport.")
    transport_json_parser.add_argument("server_id")
    transport_json_parser.add_argument("transport_type", choices=["amneziawg-conf", "vless-reality-tun", "sing-box-json", "http-proxy-tun"])
    transport_json_parser.add_argument("--config-json", required=True, help="JSON object or @path.")
    transport_json_parser.add_argument("--interface-name")
    transport_json_parser.add_argument("--source", default="")
    transport_json_parser.add_argument("--version", default="")
    transport_json_parser.add_argument("--expires-at")
    transport_json_parser.add_argument("--disabled", action="store_true")
    transport_json_parser.add_argument("--json", action="store_true", help="Print JSON.")

    transport_delete_parser = sub.add_parser("transport-delete", help="Delete a control-server transport config.")
    transport_delete_parser.add_argument("server_id")
    transport_delete_parser.add_argument("--json", action="store_true", help="Print JSON.")

    provider_refresh_parser = sub.add_parser("provider-refresh", help="Refresh VPN provider transport configs on the control-server.")
    provider_refresh_parser.add_argument("provider", choices=["vpntype", "lokvpn", "all"])
    provider_refresh_parser.add_argument("--servers", default="", help="Optional comma/space-separated server ids to refresh.")
    provider_refresh_parser.add_argument("--skip-verify", action="store_true", help="Do not probe VPNtype proxy endpoints before saving.")
    provider_refresh_parser.add_argument("--connect-timeout", type=int, default=5)
    provider_refresh_parser.add_argument("--max-time", type=int, default=12)
    provider_refresh_parser.add_argument("--json", action="store_true", help="Print JSON result.")

    import_parser = sub.add_parser("import-cudy-clients", help="Import existing cudy-home client .conf files as users.")
    import_parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "secrets" / "clients" / "cudy-home",
        help="Directory with cudy-home client .conf files.",
    )

    sync_parser = sub.add_parser("sync-cudy-clients", help="Sync live Cudy friendctl clients into users.")
    sync_parser.add_argument("--no-fetch-configs", action="store_true", help="Do not download client .conf files from Cudy.")
    sync_parser.add_argument("--json", action="store_true", help="Print JSON sync result.")

    route_plan_parser = sub.add_parser("route-plan", help="Print effective per-user route plan.")
    route_plan_parser.add_argument("--json", action="store_true", help="Print full JSON plan.")

    route_lookup_parser = sub.add_parser("route-lookup", help="Show the effective route for a domain, URL, IP, CIDR, or service alias.")
    route_lookup_parser.add_argument("target")
    route_lookup_parser.add_argument("--user-id", required=True)
    route_lookup_parser.add_argument("--json", action="store_true", help="Print full JSON lookup result.")

    service_alias_list_parser = sub.add_parser("service-alias-list", help="List service aliases used by route lookup.")
    service_alias_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    service_alias_set_parser = sub.add_parser("service-alias-set", help="Create or update a service alias.")
    service_alias_set_parser.add_argument("alias")
    service_alias_set_parser.add_argument("targets", help="Comma/space-separated target list: domains, IPs, or CIDRs.")
    service_alias_set_parser.add_argument("--label", default="", help="Display label. Defaults to alias.")
    service_alias_set_parser.add_argument("--json", action="store_true", help="Print JSON.")

    service_alias_delete_parser = sub.add_parser("service-alias-delete", help="Delete a service alias.")
    service_alias_delete_parser.add_argument("alias")
    service_alias_delete_parser.add_argument("--json", action="store_true", help="Print JSON.")

    discovery_list_parser = sub.add_parser("domain-discovery-list", help="List unknown domains discovered by route lookup.")
    discovery_list_parser.add_argument("--status", default="", help="Filter by pending, reviewed, ignored, or promoted.")
    discovery_list_parser.add_argument("--limit", type=int, default=100)
    discovery_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    discovery_record_parser = sub.add_parser("domain-discovery-record", help="Record an unknown domain in the discovery queue.")
    discovery_record_parser.add_argument("domain")
    discovery_record_parser.add_argument("--user-id", default="")
    discovery_record_parser.add_argument("--client-ip", default="")
    discovery_record_parser.add_argument("--source", default="manual")
    discovery_record_parser.add_argument("--note", default="")
    discovery_record_parser.add_argument("--json", action="store_true", help="Print JSON.")

    discovery_mark_parser = sub.add_parser("domain-discovery-mark", help="Mark a discovered domain as pending, reviewed, ignored, or promoted.")
    discovery_mark_parser.add_argument("domain")
    discovery_mark_parser.add_argument("status", choices=["pending", "reviewed", "ignored", "promoted"])
    discovery_mark_parser.add_argument("--note", default="")
    discovery_mark_parser.add_argument("--json", action="store_true", help="Print JSON.")

    discovery_promote_parser = sub.add_parser("domain-discovery-promote", help="Promote a discovered domain to an explicit Auto route.")
    discovery_promote_parser.add_argument("domain")
    discovery_promote_parser.add_argument("--user-id", default="", help="Blank creates a global domain route.")
    discovery_promote_parser.add_argument("--candidates", default="", help="Optional ordered Auto candidates, e.g. proxyde,proxynl,all-rest.")
    discovery_promote_parser.add_argument("--note", default="")
    discovery_promote_parser.add_argument("--probe-now", action="store_true", help="Create a pending Auto probe job immediately.")
    discovery_promote_parser.add_argument("--max-probe-candidates", type=int, default=4)
    discovery_promote_parser.add_argument("--json", action="store_true", help="Print JSON.")

    auto_winners_parser = sub.add_parser("auto-winners", help="Show recent Auto winners for all targets or one domain, URL, IP, CIDR, or service alias.")
    auto_winners_parser.add_argument("target", nargs="?", default="", help="Optional target filter. Blank means all recent winners.")
    auto_winners_parser.add_argument("--limit", type=int, default=10)
    auto_winners_parser.add_argument("--json", action="store_true", help="Print full JSON winner history.")

    auto_cache_list_parser = sub.add_parser("auto-cache-list", help="List cached Auto domain choices.")
    auto_cache_list_parser.add_argument("--json", action="store_true", help="Print JSON cache entries.")

    auto_cache_set_parser = sub.add_parser("auto-cache-set", help="Set cached Auto choice for one domain.")
    auto_cache_set_parser.add_argument("domain")
    auto_cache_set_parser.add_argument("selected_server_id")
    auto_cache_set_parser.add_argument("--score-ms", type=int)
    auto_cache_set_parser.add_argument("--status", default="manual")

    auto_cache_delete_parser = sub.add_parser("auto-cache-delete", help="Delete cached Auto choice for one domain.")
    auto_cache_delete_parser.add_argument("domain")

    auto_candidates_list_parser = sub.add_parser("auto-candidates-list", help="List Auto priority policies.")
    auto_candidates_list_parser.add_argument("--json", action="store_true", help="Print JSON policies.")

    auto_candidates_set_parser = sub.add_parser("auto-candidates-set", help="Set Auto priority policy.")
    auto_candidates_set_parser.add_argument("candidate_server_ids", help="Comma/space-separated server ids in priority order.")
    auto_candidates_set_parser.add_argument("--user-id", default="", help="Blank means global policy.")
    auto_candidates_set_parser.add_argument("--domain", default="", help="Blank means default policy for all domains.")
    auto_candidates_set_parser.add_argument("--disabled", action="store_true")

    auto_candidates_delete_parser = sub.add_parser("auto-candidates-delete", help="Delete Auto priority policy.")
    auto_candidates_delete_parser.add_argument("--user-id", default="", help="Blank means global policy.")
    auto_candidates_delete_parser.add_argument("--domain", default="", help="Blank means default policy for all domains.")

    auto_select_parser = sub.add_parser("auto-select", help="Probe Auto candidates for a domain and optionally save the winner.")
    auto_select_parser.add_argument("domain")
    auto_select_parser.add_argument("--user-id", default="", help="Use user/domain candidate policy resolution.")
    auto_select_parser.add_argument("--candidates", help="Override policy with comma/space-separated server ids.")
    auto_select_parser.add_argument("--url", help="Override probe URL. Default is https://DOMAIN/.")
    auto_select_parser.add_argument("--connect-timeout", type=int, default=5)
    auto_select_parser.add_argument("--max-time", type=int, default=12)
    auto_select_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    auto_select_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    auto_select_parser.add_argument("--ssh-password")
    auto_select_parser.add_argument("--ssh-timeout", type=int, default=60)
    auto_select_parser.add_argument("--switch-profiles", action="store_true", help="Allow live switching LokVPN profile candidates during the probe.")
    auto_select_parser.add_argument("--apply", action="store_true", help="Save selected winner into Auto cache. Default is probe-only.")
    auto_select_parser.add_argument("--deploy", action="store_true", help="After --apply, deploy routes to Cudy.")
    auto_select_parser.add_argument("--json", action="store_true", help="Print JSON result.")

    probe_job_create_parser = sub.add_parser("probe-job-create", help="Create an agent-side Auto probe job.")
    probe_job_create_parser.add_argument("domain")
    probe_job_create_parser.add_argument("candidate_server_ids", help="Comma/space-separated server ids in priority order.")
    probe_job_create_parser.add_argument("--user-id", default="", help="Blank means global Auto cache job.")
    probe_job_create_parser.add_argument("--assigned-device-id", default="", help="Optional exact agent device id.")
    probe_job_create_parser.add_argument("--url", help="Override probe URL. Default is https://DOMAIN/.")
    probe_job_create_parser.add_argument("--connect-timeout", type=int, default=5)
    probe_job_create_parser.add_argument("--max-time", type=int, default=12)
    probe_job_create_parser.add_argument("--priority", type=int, default=100)
    probe_job_create_parser.add_argument("--no-apply-cache", action="store_true", help="Do not update Auto cache from the winner.")
    probe_job_create_parser.add_argument("--json", action="store_true", help="Print JSON job.")

    probe_job_list_parser = sub.add_parser("probe-job-list", help="List recent agent-side Auto probe jobs.")
    probe_job_list_parser.add_argument("--limit", type=int, default=50)
    probe_job_list_parser.add_argument("--json", action="store_true", help="Print JSON jobs.")

    probe_job_reset_parser = sub.add_parser("probe-job-reset", help="Reset or fail matching agent-side Auto probe jobs.")
    probe_job_reset_parser.add_argument("--status", default="running", choices=["pending", "running", "failed", "done"])
    probe_job_reset_parser.add_argument("--target-status", default="pending", choices=["pending", "failed"])
    probe_job_reset_parser.add_argument("--older-than-seconds", type=int, default=0)
    probe_job_reset_parser.add_argument("--domain", default="")
    probe_job_reset_parser.add_argument("--assigned-device-id", default="")
    probe_job_reset_parser.add_argument("--json", action="store_true", help="Print JSON result.")

    auto_worker_once_parser = sub.add_parser("auto-worker-once", help="Create due agent Auto probe jobs once.")
    auto_worker_once_parser.add_argument("--cache-ttl-seconds", type=int, default=3600)
    auto_worker_once_parser.add_argument("--job-stale-seconds", type=int, default=900)
    auto_worker_once_parser.add_argument("--agent-stale-seconds", type=int, default=600)
    auto_worker_once_parser.add_argument("--max-jobs", type=int, default=5)
    auto_worker_once_parser.add_argument("--max-candidates-per-job", type=int, default=4)
    auto_worker_once_parser.add_argument("--connect-timeout", type=int, default=5)
    auto_worker_once_parser.add_argument("--max-time", type=int, default=12)
    auto_worker_once_parser.add_argument("--active-domain-limit", type=int, default=300)
    auto_worker_once_parser.add_argument("--json", action="store_true", help="Print JSON result.")

    export_parser = sub.add_parser(
        "export-pbr-overrides",
        help="Export global admin routes as OpenWrt /etc/pbr-overrides force-<interface>.domains files.",
    )
    export_parser.add_argument("--output-dir", type=Path, default=DEFAULT_PBR_EXPORT_DIR)
    export_parser.add_argument("--json", action="store_true", help="Print full export manifest.")

    deploy_parser = sub.add_parser(
        "deploy-pbr-overrides",
        help="Export global PBR overrides and optionally deploy them to Cudy over SSH.",
    )
    deploy_parser.add_argument("--output-dir", type=Path, default=DEFAULT_PBR_EXPORT_DIR)
    deploy_parser.add_argument("--remote-dir", default=REMOTE_PBR_DIR)
    deploy_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    deploy_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    deploy_parser.add_argument("--ssh-password")
    deploy_parser.add_argument("--ssh-timeout", type=int, default=60)
    deploy_parser.add_argument("--install-scripts", action="store_true", help="Install updated PBR and vpn-switch scripts.")
    deploy_parser.add_argument("--no-restart-pbr", action="store_true", help="Upload files but do not restart PBR.")
    deploy_parser.add_argument("--prune-empty", action="store_true", help="Upload empty generated files to clear old generated routes.")
    deploy_parser.add_argument("--allow-empty", action="store_true", help="Allow apply when there are zero global routes.")
    deploy_parser.add_argument("--apply", action="store_true", help="Actually upload/apply on Cudy. Default is dry-run.")
    deploy_parser.add_argument("--json", action="store_true", help="Print JSON plan/result.")

    user_export_parser = sub.add_parser(
        "export-user-routes",
        help="Export per-user domain routes as source-IP route input for Cudy.",
    )
    user_export_parser.add_argument("--output-dir", type=Path, default=DEFAULT_USER_ROUTES_EXPORT_DIR)
    user_export_parser.add_argument("--json", action="store_true", help="Print full export manifest.")

    user_domain_list_parser = sub.add_parser("user-domain-route-list", help="List per-user domain routes.")
    user_domain_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    user_domain_set_parser = sub.add_parser("user-domain-route-set", help="Set a per-user domain route.")
    user_domain_set_parser.add_argument("user_id")
    user_domain_set_parser.add_argument("domain")
    user_domain_set_parser.add_argument("server_id")
    user_domain_set_parser.add_argument("--disabled", action="store_true")
    user_domain_set_parser.add_argument("--json", action="store_true", help="Print JSON.")

    user_domain_delete_parser = sub.add_parser("user-domain-route-delete", help="Delete a per-user domain route.")
    user_domain_delete_parser.add_argument("user_id")
    user_domain_delete_parser.add_argument("domain")
    user_domain_delete_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_domain_list_parser = sub.add_parser("global-domain-route-list", help="List global domain routes.")
    global_domain_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_domain_set_parser = sub.add_parser("global-domain-route-set", help="Set a global domain route.")
    global_domain_set_parser.add_argument("domain")
    global_domain_set_parser.add_argument("server_id")
    global_domain_set_parser.add_argument("--disabled", action="store_true")
    global_domain_set_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_domain_delete_parser = sub.add_parser("global-domain-route-delete", help="Delete a global domain route.")
    global_domain_delete_parser.add_argument("domain")
    global_domain_delete_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_domain_import_parser = sub.add_parser("global-domain-route-import", help="Import global domain routes from tunnel-list files.")
    global_domain_import_parser.add_argument("server_id")
    global_domain_import_parser.add_argument("input_files", nargs="+", type=Path)
    global_domain_import_parser.add_argument("--disabled", action="store_true")
    global_domain_import_parser.add_argument("--json", action="store_true", help="Print JSON.")

    user_ip_list_parser = sub.add_parser("user-ip-route-list", help="List per-user IPv4/CIDR routes.")
    user_ip_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    user_ip_set_parser = sub.add_parser("user-ip-route-set", help="Set a per-user IPv4/CIDR route.")
    user_ip_set_parser.add_argument("user_id")
    user_ip_set_parser.add_argument("target_cidr")
    user_ip_set_parser.add_argument("server_id")
    user_ip_set_parser.add_argument("--disabled", action="store_true")
    user_ip_set_parser.add_argument("--json", action="store_true", help="Print JSON.")

    user_ip_delete_parser = sub.add_parser("user-ip-route-delete", help="Delete a per-user IPv4/CIDR route.")
    user_ip_delete_parser.add_argument("user_id")
    user_ip_delete_parser.add_argument("target_cidr")
    user_ip_delete_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_ip_list_parser = sub.add_parser("global-ip-route-list", help="List global IPv4/CIDR routes.")
    global_ip_list_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_ip_set_parser = sub.add_parser("global-ip-route-set", help="Set a global IPv4/CIDR route.")
    global_ip_set_parser.add_argument("target_cidr")
    global_ip_set_parser.add_argument("server_id")
    global_ip_set_parser.add_argument("--disabled", action="store_true")
    global_ip_set_parser.add_argument("--source", default="")
    global_ip_set_parser.add_argument("--note", default="")
    global_ip_set_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_ip_delete_parser = sub.add_parser("global-ip-route-delete", help="Delete a global IPv4/CIDR route.")
    global_ip_delete_parser.add_argument("target_cidr")
    global_ip_delete_parser.add_argument("--json", action="store_true", help="Print JSON.")

    global_ip_import_parser = sub.add_parser("global-ip-route-import", help="Import global IPv4/CIDR routes from override files.")
    global_ip_import_parser.add_argument("server_id")
    global_ip_import_parser.add_argument("input_files", nargs="+", type=Path)
    global_ip_import_parser.add_argument("--source", default="override-file")
    global_ip_import_parser.add_argument("--note", default="")
    global_ip_import_parser.add_argument("--disabled", action="store_true")
    global_ip_import_parser.add_argument("--json", action="store_true", help="Print JSON.")

    user_deploy_parser = sub.add_parser(
        "deploy-user-routes",
        help="Export and optionally deploy per-user source-IP routes to Cudy.",
    )
    user_deploy_parser.add_argument("--output-dir", type=Path, default=DEFAULT_USER_ROUTES_EXPORT_DIR)
    user_deploy_parser.add_argument("--remote-dir", default=REMOTE_USER_ROUTES_DIR)
    user_deploy_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    user_deploy_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    user_deploy_parser.add_argument("--ssh-password")
    user_deploy_parser.add_argument("--ssh-timeout", type=int, default=60)
    user_deploy_parser.add_argument("--install-script", action="store_true", help="Install /usr/bin/cudy-user-routes-apply.")
    user_deploy_parser.add_argument("--no-run-apply", action="store_true", help="Upload files but do not run the apply script.")
    user_deploy_parser.add_argument("--allow-empty", action="store_true", help="Allow apply when there are zero user routes.")
    user_deploy_parser.add_argument("--apply", action="store_true", help="Actually upload/apply on Cudy. Default is dry-run.")
    user_deploy_parser.add_argument("--json", action="store_true", help="Print JSON plan/result.")

    user_status_parser = sub.add_parser(
        "status-user-routes",
        help="Read deployed per-user source-IP route status from Cudy.",
    )
    user_status_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    user_status_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    user_status_parser.add_argument("--ssh-password")
    user_status_parser.add_argument("--ssh-timeout", type=int, default=30)
    user_status_parser.add_argument("--json", action="store_true", help="Print JSON status.")

    full_deploy_parser = sub.add_parser(
        "deploy-routes",
        help="Export and optionally deploy global and per-user routes to Cudy.",
    )
    full_deploy_parser.add_argument("--pbr-output-dir", type=Path, default=DEFAULT_PBR_EXPORT_DIR)
    full_deploy_parser.add_argument("--user-output-dir", type=Path, default=DEFAULT_USER_ROUTES_EXPORT_DIR)
    full_deploy_parser.add_argument("--pbr-remote-dir", default=REMOTE_PBR_DIR)
    full_deploy_parser.add_argument("--user-remote-dir", default=REMOTE_USER_ROUTES_DIR)
    full_deploy_parser.add_argument("--ssh-host", default=DEFAULT_CUDY_HOST)
    full_deploy_parser.add_argument("--ssh-user", default=DEFAULT_CUDY_USER)
    full_deploy_parser.add_argument("--ssh-password")
    full_deploy_parser.add_argument("--ssh-timeout", type=int, default=60)
    full_deploy_parser.add_argument("--install-scripts", action="store_true", help="Install PBR, vpn-switch, and user-route scripts.")
    full_deploy_parser.add_argument("--no-restart-pbr", action="store_true", help="Upload global PBR files but do not restart PBR.")
    full_deploy_parser.add_argument("--no-run-user-apply", action="store_true", help="Upload user route files but do not run the apply script.")
    full_deploy_parser.add_argument("--prune-empty", action="store_true", help="Upload empty generated global PBR files to clear old generated routes.")
    full_deploy_parser.add_argument("--allow-empty", action="store_true", help="Allow apply when a route layer has zero routes.")
    full_deploy_parser.add_argument("--apply", action="store_true", help="Actually upload/apply on Cudy. Default is dry-run.")
    full_deploy_parser.add_argument("--json", action="store_true", help="Print JSON plan/result.")

    serve_parser = sub.add_parser("serve", help="Run local web server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--no-auto-worker", action="store_true", help="Disable background Auto probe job scheduler.")
    serve_parser.add_argument("--auto-worker-interval", type=int, default=300)
    serve_parser.add_argument("--auto-cache-ttl-seconds", type=int, default=3600)
    serve_parser.add_argument("--auto-worker-job-stale-seconds", type=int, default=900)
    serve_parser.add_argument("--auto-worker-agent-stale-seconds", type=int, default=600)
    serve_parser.add_argument("--auto-worker-max-jobs", type=int, default=5)
    serve_parser.add_argument("--auto-worker-max-candidates-per-job", type=int, default=4)
    serve_parser.add_argument("--auto-worker-connect-timeout", type=int, default=5)
    serve_parser.add_argument("--auto-worker-max-time", type=int, default=12)
    serve_parser.add_argument("--auto-worker-active-domain-limit", type=int, default=300)
    serve_parser.add_argument("--no-provider-refresh-worker", action="store_true", help="Disable background VPNtype/LokVPN transport refresh.")
    serve_parser.add_argument("--provider-refresh-provider", choices=["vpntype", "lokvpn", "all"], default="all")
    serve_parser.add_argument("--provider-refresh-interval", type=int, default=900)
    serve_parser.add_argument("--provider-refresh-skip-verify", action="store_true")
    serve_parser.add_argument("--provider-refresh-connect-timeout", type=int, default=5)
    serve_parser.add_argument("--provider-refresh-max-time", type=int, default=12)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init-db":
        init_db(args.db, args.inventory, reset_from_inventory=args.reset_from_inventory)
        print_db_summary(args.db)
        return 0
    if args.command == "summary":
        init_db(args.db, args.inventory)
        print_db_summary(args.db)
        return 0
    if args.command == "control-endpoints":
        manifest = control_endpoints_manifest()
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            print(f"generation: {manifest['generation']}")
            for endpoint in manifest["endpoints"]:
                print(f"{endpoint['role']:8} priority={endpoint['priority']:3} url={endpoint['url']}")
        return 0
    if args.command in {"system-status", "status"}:
        status = build_system_status(args.db, args.inventory)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"ok: {status['ok']}")
            print(f"uptime_seconds: {status['service']['uptime_seconds']}")
            print(f"agents: online={status['agents']['online']} enabled={status['agents']['enabled']} total={status['agents']['total']}")
            print(f"probe_jobs: {status['probe_jobs']['by_status']}")
            print(
                f"transports: enabled={status['transports']['enabled']} total={status['transports']['total']} "
                f"oldest_age={status['transports']['oldest_age_seconds']}"
            )
            for name, worker in status["workers"].items():
                print(
                    f"worker.{name}: enabled={worker.get('enabled')} "
                    f"last_finished_age={worker.get('last_finished_age_seconds')} "
                    f"error={worker.get('last_error') or '-'}"
                )
            fallback = status["control"]["cudy_fallback_state"]
            print(f"cudy_fallback: reachable={fallback.get('reachable')} ok={fallback.get('ok')} age={fallback.get('age_seconds')}")
            backup = status["operations"]["local_backup"]["latest_archive"]
            print(f"local_backup: exists={backup.get('exists')} age={backup.get('age_seconds')} name={backup.get('name') or '-'}")
            sync_log = status["operations"]["local_cudy_fallback_sync"]["task_log"]
            print(f"local_fallback_sync_log: exists={sync_log.get('exists')} age={sync_log.get('age_seconds')}")
            for warning in status["warnings"]:
                print(f"warning: {warning}")
            for advisory in status.get("advisories") or []:
                print(f"advisory: {advisory}")
        return 0 if status["ok"] or not args.strict else 2
    if args.command == "create-user":
        password = None if args.no_password_change else read_password_arg(args.password, confirm=args.password is None)
        create_or_update_user(
            args.db,
            args.inventory,
            user_id=args.user_id,
            display_name=args.display_name or args.user_id,
            role=args.role,
            password=password,
            client_ip=args.client_ip,
            enabled=not args.disabled,
            allow_no_password=args.no_password_change,
        )
        print(f"User saved: {args.user_id} role={args.role}")
        return 0
    if args.command == "service-user-create":
        create_or_update_user(
            args.db,
            args.inventory,
            user_id=args.user_id,
            display_name=args.display_name or args.user_id,
            role="user",
            password=None,
            client_ip=args.client_ip,
            enabled=not args.disabled,
            allow_no_password=True,
        )
        result = {
            "id": args.user_id,
            "display_name": args.display_name or args.user_id,
            "role": "user",
            "client_ip": args.client_ip or "",
            "enabled": not args.disabled,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Service user saved: {args.user_id} role=user enabled={not args.disabled}")
        return 0
    if args.command == "device-create":
        result = create_agent_device(
            args.db,
            args.inventory,
            user_id=args.user_id,
            device_id=args.device_id,
            display_name=args.display_name,
            platform=args.platform,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Device saved: {result['id']} user={result['user_id']} platform={result['platform']}")
            print("Token is shown once. Store it on the client device:")
            print(result["token"])
        return 0
    if args.command == "device-list":
        entries = list_agent_devices(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No agent devices.")
            for item in entries:
                print(
                    f"{item['id']}\tuser={item['user_id']}\tplatform={item['platform'] or '-'}\t"
                    f"enabled={bool(item['enabled'])}\tlast_seen={item['last_seen_at'] or '-'}"
                )
        return 0
    if args.command == "device-revoke":
        result = revoke_agent_device(args.db, args.inventory, device_id=args.device_id)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Device revoked: {result['device_id']}")
        return 0
    if args.command == "device-status":
        entries = agent_status_rows(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No agent devices.")
            for item in entries:
                health = item.get("status", {}).get("health", {})
                print(
                    f"{item['device_id']}\tuser={item['user_id']}\t"
                    f"last_seen={item['last_seen_at'] or '-'}\t"
                    f"reported={item['reported_at'] or '-'}\thealth={health}"
                )
        return 0
    if args.command == "enrollment-create":
        result = create_agent_enrollment_code(
            args.db,
            args.inventory,
            user_id=args.user_id,
            device_id=args.device_id,
            display_name=args.display_name,
            platform=args.platform,
            ttl_hours=args.ttl_hours,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Enrollment code created: {result['id']} user={result['user_id']} expires={result['expires_at']}")
            print("Code is shown once. Give it to the user:")
            print(result["code"])
        return 0
    if args.command == "enrollment-list":
        entries = list_agent_enrollment_codes(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No enrollment codes.")
            for item in entries:
                state = "used" if item.get("used_at") else ("enabled" if item.get("enabled") else "disabled")
                print(
                    f"{item['id']}\tuser={item['user_id']}\tplatform={item['platform'] or '-'}\t"
                    f"state={state}\texpires={item['expires_at'] or '-'}\tdevice={item.get('used_device_id') or item.get('desired_device_id') or '-'}"
                )
        return 0
    if args.command == "enrollment-revoke":
        result = revoke_agent_enrollment_code(args.db, args.inventory, code_id=args.code_id)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Enrollment code revoked: {result['id']}")
        return 0
    if args.command == "transport-list":
        init_db(args.db, args.inventory)
        with connect(args.db) as conn:
            entries = transport_config_rows(conn)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No transport configs.")
            for item in entries:
                print(
                    f"{item['server_id']}\t{item['transport_type']}\tiface={item['interface_name']}\t"
                    f"enabled={bool(item['enabled'])}\tsource={item.get('source') or '-'}\t"
                    f"updated={item.get('updated_at') or '-'}"
                )
        return 0
    if args.command == "transport-set-http":
        if args.proxy_port <= 0 or args.proxy_port > 65535:
            raise ValueError("proxy port must be 1-65535")
        result = save_transport_config(
            args.db,
            args.inventory,
            server_id=args.server_id,
            transport_type="http-proxy-tun",
            interface_name=args.interface_name,
            config={
                "proxy_type": args.proxy_type,
                "server": args.proxy_host,
                "server_port": args.proxy_port,
            },
            enabled=not args.disabled,
            source=args.source,
            version=args.version,
            expires_at=args.expires_at,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Transport saved: {result['server_id']} {result['transport_type']} iface={result['interface_name']}")
        return 0
    if args.command == "transport-set-json":
        result = save_transport_config(
            args.db,
            args.inventory,
            server_id=args.server_id,
            transport_type=args.transport_type,
            interface_name=args.interface_name,
            config=read_json_arg(args.config_json),
            enabled=not args.disabled,
            source=args.source,
            version=args.version,
            expires_at=args.expires_at,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Transport saved: {result['server_id']} {result['transport_type']} iface={result['interface_name']}")
        return 0
    if args.command == "transport-delete":
        result = delete_transport_config(args.db, args.inventory, server_id=args.server_id)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Transport deleted: {result['server_id']}")
        return 0
    if args.command == "provider-refresh":
        server_ids = parse_candidate_server_ids(args.servers) if args.servers else None
        result = refresh_provider_transports(
            args.db,
            args.inventory,
            provider=args.provider,
            server_ids=server_ids,
            skip_verify=args.skip_verify,
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            groups = result.get("results") if result.get("provider") == "all" else [result]
            for group in groups or []:
                print(
                    f"{group['provider']}: refreshed={len(group.get('refreshed') or [])} "
                    f"failed={len(group.get('failed') or [])}"
                )
                for item in group.get("refreshed") or []:
                    print(
                        f"  OK {item['server_id']}\t{item.get('endpoint') or '-'}\t"
                        f"{item.get('updated_at') or '-'}"
                    )
                for item in group.get("failed") or []:
                    print(f"  FAIL {item['server_id']}\t{item.get('error') or '-'}")
        return 0
    if args.command == "import-cudy-clients":
        imported = import_cudy_clients(args.db, args.inventory, args.source_dir)
        for item in imported:
            print(f"{item['id']}\t{item['client_ip']}\t{item['source']}")
        print(f"Imported/updated users: {len(imported)}")
        return 0
    if args.command == "sync-cudy-clients":
        result = sync_cudy_clients_from_router(
            args.db,
            args.inventory,
            fetch_configs=not args.no_fetch_configs,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for item in result["synced"]:
                print(f"{item['user_id']}\t{item['client_ip']}\tenabled={item['enabled']}\tconfig={item['config_path'] or '-'}")
            print(f"Synced Cudy clients: {len(result['synced'])}")
            for warning in result["warnings"]:
                print(f"WARNING: {warning}")
        return 0
    if args.command == "route-plan":
        init_db(args.db, args.inventory)
        plan = build_route_plan(args.db)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print(
                f"users={plan['summary']['users']} "
                f"global_routes={plan['summary']['global_routes']} "
                f"effective_routes={plan['summary']['effective_routes']} "
                f"warnings={plan['summary']['warnings']}"
            )
            for user in plan["users"]:
                print(
                    f"{user['id']}\tclient_ip={user['client_ip'] or '-'}\t"
                    f"default={user['default_server_id']}\troutes={len(user['routes'])}"
                )
                for route in user["routes"]:
                    requested = route.get("requested_server_id")
                    via = f" requested={requested}" if requested and requested != route["server_id"] else ""
                    print(f"  {route['domain']}\t{route['server_id']}\t{route['source']}{via}")
                for warning in user["warnings"]:
                    print(f"  WARNING: {warning}")
        return 0
    if args.command == "route-lookup":
        result = route_lookup(args.db, args.inventory, user_id=args.user_id, target=args.target)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result.get("alias"):
                alias = result["alias"]
                print(f"alias={alias['alias']} label={alias['label']} targets={len(alias.get('targets') or [])}")
            for item in result["results"]:
                rule = item.get("matched_rule") or {}
                rule_label = rule.get("target_cidr") or rule.get("domain") or "-"
                auto = item.get("auto_cache") or {}
                auto_label = ""
                if auto:
                    auto_label = f" auto={auto.get('selected_server_id') or '-'} score={auto.get('score_ms') if auto.get('score_ms') is not None else '-'}"
                print(
                    f"{item['target']}\tstate={item['route_state']}\tserver={item['server_id']}"
                    f"\trule={rule_label}{auto_label}"
                )
        return 0
    if args.command == "service-alias-list":
        init_db(args.db, args.inventory)
        with connect(args.db) as conn:
            entries = service_alias_rows(conn)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No service aliases.")
            for item in entries:
                print(f"{item['alias']}\tlabel={item['label']}\ttargets={','.join(item.get('targets') or [])}")
        return 0
    if args.command == "service-alias-set":
        result = save_service_alias(
            args.db,
            args.inventory,
            alias=args.alias,
            label=args.label or args.alias,
            targets=args.targets,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"{result['alias']}\tlabel={result['label']}\ttargets={','.join(result.get('targets') or [])}")
        return 0
    if args.command == "service-alias-delete":
        result = delete_service_alias(args.db, args.inventory, alias=args.alias)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"deleted={result['alias']}")
        return 0
    if args.command == "domain-discovery-list":
        init_db(args.db, args.inventory)
        with connect(args.db) as conn:
            entries = domain_discovery_rows(conn, status=args.status, limit=args.limit)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No discovered domains.")
            for item in entries:
                print(
                    f"{item['domain']}\tstatus={item['status']}\thits={item['hit_count']}\t"
                    f"users={','.join(item.get('user_ids') or []) or '-'}\t"
                    f"clients={','.join(item.get('client_ips') or []) or '-'}\t"
                    f"last_seen={item['last_seen_at']}"
                )
        return 0
    if args.command == "domain-discovery-record":
        init_db(args.db, args.inventory)
        with connect(args.db) as conn:
            item = record_domain_discovery(
                conn,
                domain=args.domain,
                user_id=args.user_id,
                client_ip=args.client_ip,
                source=args.source,
                note=args.note,
            )
        if args.json:
            print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            print(f"Discovered domain recorded: {item['domain']} status={item['status']} hits={item['hit_count']}")
        return 0
    if args.command == "domain-discovery-mark":
        result = save_domain_discovery_status(
            args.db,
            args.inventory,
            domain=args.domain,
            status=args.status,
            note=args.note,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            item = result["item"]
            print(f"Discovered domain marked: {item['domain']} status={item['status']}")
        return 0
    if args.command == "domain-discovery-promote":
        result = promote_domain_discovery_to_auto_route(
            args.db,
            args.inventory,
            domain=args.domain,
            user_id=args.user_id,
            candidate_server_ids=args.candidates,
            note=args.note,
            probe_now=args.probe_now,
            max_probe_candidates=args.max_probe_candidates,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            policy = result.get("auto_candidate_policy") or {}
            candidates = ",".join(policy.get("candidate_server_ids") or [])
            suffix = f" candidates={candidates}" if candidates else " candidates=inherited"
            print(
                f"Discovered domain promoted: {result['domain']} "
                f"scope={result['route_scope']} server=auto{suffix}"
            )
            probe_job = result.get("probe_job") or {}
            if probe_job.get("created"):
                print(f"Probe job created: {probe_job['created']['id']}")
            elif probe_job.get("skipped"):
                skipped = probe_job["skipped"]
                print(f"Probe job skipped: {skipped.get('reason')} {skipped.get('job_id') or ''}".rstrip())
        return 0
    if args.command == "auto-winners":
        result = recent_auto_winners(args.db, args.inventory, target=args.target, limit=args.limit)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result.get("target"):
                keys = ",".join(result.get("cache_keys") or []) or "-"
                print(f"Auto winners target={result['target']} cache_keys={keys}")
            else:
                print("Auto winners: recent global history")
            winners = result.get("winners") or []
            if not winners:
                print("No Auto winners.")
            for item in winners:
                speed = item.get("speed_mbps")
                latency = item.get("latency_ms")
                print(
                    f"{item['domain']}\tuser={item.get('user_id') or '-'}\t"
                    f"winner={item.get('winner_server_id') or '-'}\t"
                    f"latency={latency if latency is not None else '-'}ms\t"
                    f"speed={speed if speed is not None else '-'}Mbps\t"
                    f"agent={item.get('claimed_by_device_id') or '-'}\t"
                    f"updated={item.get('finished_at') or item.get('updated_at') or '-'}"
                )
        return 0
    if args.command == "auto-cache-list":
        init_db(args.db, args.inventory)
        with connect(args.db) as conn:
            entries = rows(
                conn,
                """
                SELECT domain, selected_server_id, score_ms, status, checked_at
                FROM domain_auto_cache
                ORDER BY domain
                """,
            )
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("Auto cache is empty.")
            for item in entries:
                print(
                    f"{item['domain']}\t{item['selected_server_id'] or '-'}\t"
                    f"score={item['score_ms'] if item['score_ms'] is not None else '-'}\t"
                    f"status={item['status']}\tchecked={item['checked_at'] or '-'}"
                )
        return 0
    if args.command == "auto-cache-set":
        item = save_auto_cache_entry(
            args.db,
            args.inventory,
            domain=args.domain,
            selected_server_id=args.selected_server_id,
            score_ms=args.score_ms,
            status=args.status,
        )
        print(
            f"Auto cache saved: {item['domain']} -> {item['selected_server_id']} "
            f"score={item['score_ms'] if item['score_ms'] is not None else '-'} status={item['status']}"
        )
        return 0
    if args.command == "auto-cache-delete":
        item = delete_auto_cache_entry(args.db, args.inventory, args.domain)
        print(f"Auto cache deleted: {item['domain']}")
        return 0
    if args.command == "auto-candidates-list":
        init_db(args.db, args.inventory)
        with connect(args.db) as conn:
            entries = auto_candidate_policy_rows(conn)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("Auto priority policies are empty.")
            for item in entries:
                print(
                    f"{item['scope']}\tuser={item['user_id'] or '-'}\t"
                    f"domain={item['domain'] or '*'}\t"
                    f"servers={','.join(item['candidate_server_ids'])}\t"
                    f"enabled={bool(item['enabled'])}"
                )
        return 0
    if args.command == "auto-candidates-set":
        item = save_auto_candidate_policy(
            args.db,
            args.inventory,
            user_id=args.user_id,
            domain=args.domain,
            candidate_server_ids=args.candidate_server_ids,
            enabled=not args.disabled,
        )
        print(
            f"Auto priority policy saved: {item['scope']} user={item['user_id'] or '-'} "
            f"domain={item['domain'] or '*'} servers={','.join(item['candidate_server_ids'])}"
        )
        return 0
    if args.command == "auto-candidates-delete":
        item = delete_auto_candidate_policy(
            args.db,
            args.inventory,
            user_id=args.user_id,
            domain=args.domain,
        )
        print(f"Auto priority policy deleted: {item['scope']} user={item['user_id'] or '-'} domain={item['domain'] or '*'}")
        return 0
    if args.command == "auto-select":
        result = auto_select_domain(
            args.db,
            args.inventory,
            domain=args.domain,
            user_id=args.user_id,
            candidate_server_ids=args.candidates,
            url=args.url,
            apply=args.apply,
            deploy=args.deploy,
            switch_profiles=args.switch_profiles,
            ssh_host=args.ssh_host,
            ssh_user=args.ssh_user,
            ssh_password=args.ssh_password,
            ssh_timeout=args.ssh_timeout,
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"Auto select {result['domain']} policy={result['policy_source']} "
                f"candidates={','.join(result['candidate_server_ids'])}"
            )
            for check in result["checks"]:
                score = check.get("score_ms")
                print(
                    f"  {check['server_id']}\t{check['status']}\t"
                    f"http={check.get('http_code', '-')}\t"
                    f"score={score if score is not None else '-'}ms\t"
                    f"iface={check.get('interface') or '-'}"
                )
            winner = result.get("winner")
            if winner:
                print(f"Winner: {winner['server_id']} score={winner.get('score_ms')}ms")
                if result.get("applied"):
                    print("Auto cache updated.")
                if result.get("deployed"):
                    print("Routes deployed.")
            else:
                print("No working candidate found.")
                return 1
        return 0
    if args.command == "probe-job-create":
        result = create_probe_job(
            args.db,
            args.inventory,
            domain=args.domain,
            candidate_server_ids=args.candidate_server_ids,
            user_id=args.user_id,
            url=args.url,
            assigned_device_id=args.assigned_device_id,
            apply_cache=not args.no_apply_cache,
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
            priority=args.priority,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"Probe job created: {result['id']} domain={result['domain']} "
                f"candidates={','.join(result['candidate_server_ids'])} "
                f"assigned={result.get('assigned_device_id') or '-'} apply_cache={result.get('apply_cache')}"
            )
        return 0
    if args.command == "probe-job-list":
        entries = list_probe_jobs(args.db, args.inventory, limit=args.limit)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No probe jobs.")
            for item in entries:
                print(
                    f"{item['id']}\t{item['status']}\t{item['domain']}\t"
                    f"candidates={','.join(item['candidate_server_ids'])}\t"
                    f"assigned={item.get('assigned_device_id') or '-'}\t"
                    f"claimed={item.get('claimed_by_device_id') or '-'}\t"
                    f"winner={item.get('winner_server_id') or '-'}\t"
                    f"score={item.get('score_ms') if item.get('score_ms') is not None else '-'}\t"
                    f"updated={item.get('updated_at') or '-'}"
                )
        return 0
    if args.command == "probe-job-reset":
        result = reset_probe_jobs(
            args.db,
            args.inventory,
            status=args.status,
            older_than_seconds=args.older_than_seconds,
            domain=args.domain,
            assigned_device_id=args.assigned_device_id,
            target_status=args.target_status,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"Probe jobs reset: matched={result['matched']} "
                f"{result['from_status']} -> {result['target_status']}"
            )
        return 0
    if args.command == "auto-worker-once":
        result = create_auto_probe_jobs_once(
            args.db,
            args.inventory,
            cache_ttl_seconds=args.cache_ttl_seconds,
            job_stale_seconds=args.job_stale_seconds,
            agent_stale_seconds=args.agent_stale_seconds,
            max_jobs=args.max_jobs,
            max_candidates_per_job=args.max_candidates_per_job,
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
            active_domain_limit=args.active_domain_limit,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"Auto worker once: created={len(result['created'])} "
                f"skipped={len(result['skipped'])} active_agents={result['active_agents']}"
            )
            for item in result["created"]:
                print(
                    f"  created {item['id']} {item['domain']} "
                    f"candidates={','.join(item['candidate_server_ids'])} "
                    f"assigned={item.get('assigned_device_id') or '-'}"
                )
            for item in result["skipped"][:20]:
                print(f"  skipped {item['domain']} user={item.get('user_id') or '-'} reason={item.get('reason')}")
        return 0
    if args.command == "user-domain-route-list":
        entries = list_user_domain_routes(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No user domain routes.")
            for item in entries:
                print(
                    f"{item['user_id']}\t{item['client_ip'] or '-'}\t"
                    f"{item['domain']}\t{item['server_id']}\tenabled={bool(item['enabled'])}"
                )
        return 0
    if args.command == "user-domain-route-set":
        result = save_user_domain_route(
            args.db,
            args.inventory,
            user_id=args.user_id,
            domain=args.domain,
            server_id=args.server_id,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"User domain route saved: {result['user_id']} {result['domain']} -> {result['server_id']}")
        return 0
    if args.command == "user-domain-route-delete":
        result = delete_user_domain_route(args.db, args.inventory, user_id=args.user_id, domain=args.domain)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"User domain route deleted: {result['user_id']} {result['domain']}")
        return 0
    if args.command == "global-domain-route-list":
        entries = list_global_domain_routes(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        elif not entries:
            print("Global domain routes are empty.")
        else:
            for item in entries:
                print(f"{item['domain']}\t{item['server_id']}\tenabled={bool(item['enabled'])}")
        return 0
    if args.command == "global-domain-route-set":
        result = save_global_domain_route(
            args.db,
            args.inventory,
            domain=args.domain,
            server_id=args.server_id,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Global domain route saved: {result['domain']} -> {result['server_id']}")
        return 0
    if args.command == "global-domain-route-delete":
        result = delete_global_domain_route(args.db, args.inventory, domain=args.domain)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Global domain route deleted: {result['domain']}")
        return 0
    if args.command == "global-domain-route-import":
        result = import_global_domain_routes(
            args.db,
            args.inventory,
            input_files=args.input_files,
            server_id=args.server_id,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Imported {result['count']} global domain route(s) -> {result['server_id']}")
        return 0
    if args.command == "user-ip-route-list":
        entries = list_user_ip_routes(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No user IP/CIDR routes.")
            for item in entries:
                print(
                    f"{item['user_id']}\t{item['client_ip'] or '-'}\t"
                    f"{item['target_cidr']}\t{item['server_id']}\tenabled={bool(item['enabled'])}"
                )
        return 0
    if args.command == "user-ip-route-set":
        result = save_user_ip_route(
            args.db,
            args.inventory,
            user_id=args.user_id,
            target_cidr=args.target_cidr,
            server_id=args.server_id,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"User IP route saved: {result['user_id']} {result['target_cidr']} -> {result['server_id']}")
        return 0
    if args.command == "user-ip-route-delete":
        result = delete_user_ip_route(args.db, args.inventory, user_id=args.user_id, target_cidr=args.target_cidr)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"User IP route deleted: {result['user_id']} {result['target_cidr']}")
        return 0
    if args.command == "global-ip-route-list":
        entries = list_global_ip_routes(args.db, args.inventory)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        elif not entries:
            print("Global IP/CIDR routes are empty.")
        else:
            for item in entries:
                print(
                    f"{item['target_cidr']}\t{item['server_id']}\t"
                    f"enabled={bool(item['enabled'])}\tsource={item.get('source') or '-'}"
                )
        return 0
    if args.command == "global-ip-route-set":
        result = save_global_ip_route(
            args.db,
            args.inventory,
            target_cidr=args.target_cidr,
            server_id=args.server_id,
            enabled=not args.disabled,
            source=args.source,
            note=args.note,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Global IP route saved: {result['target_cidr']} -> {result['server_id']}")
        return 0
    if args.command == "global-ip-route-delete":
        result = delete_global_ip_route(args.db, args.inventory, target_cidr=args.target_cidr)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Global IP route deleted: {result['target_cidr']}")
        return 0
    if args.command == "global-ip-route-import":
        result = import_global_ip_routes(
            args.db,
            args.inventory,
            input_files=args.input_files,
            server_id=args.server_id,
            source=args.source,
            note=args.note,
            enabled=not args.disabled,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Imported {result['count']} global IP route(s) -> {result['server_id']}")
        return 0
    if args.command == "export-pbr-overrides":
        manifest = export_pbr_overrides(args.db, args.inventory, args.output_dir)
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            route_count = len(manifest["exported_routes"])
            non_empty = [item for item in manifest["files"] if item["domains"]]
            print(f"Exported global routes: {route_count}")
            print(f"Output: {manifest['output_dir']}")
            print(f"Files: {len(manifest['files'])} total, {len(non_empty)} non-empty")
            for item in non_empty:
                print(f"  {item['name']}\tdomains={item['domains']}")
            for warning in manifest["warnings"]:
                print(f"WARNING: {warning}")
        return 0
    if args.command == "deploy-pbr-overrides":
        manifest = export_pbr_overrides(args.db, args.inventory, args.output_dir)
        plan = build_pbr_deploy_plan(
            manifest,
            ssh_host=args.ssh_host,
            remote_dir=args.remote_dir,
            install_scripts=args.install_scripts,
            restart_pbr=not args.no_restart_pbr,
            prune_empty=args.prune_empty,
        )
        if not args.apply:
            payload = {"apply": False, "manifest": manifest, "plan": plan}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("Dry run. Use --apply to deploy to Cudy.")
                print(f"Global routes: {plan['route_count']}")
                print(f"Remote: {args.ssh_user}@{args.ssh_host}:{args.remote_dir}")
                print(f"Uploads: {plan['upload_count']}")
                print(f"Install scripts: {bool(args.install_scripts)}")
                print(f"Restart PBR: {not args.no_restart_pbr}")
                print(f"Prune empty files: {bool(args.prune_empty)}")
                for upload in plan["uploads"]:
                    print(f"  {upload['local']} -> {upload['remote']}")
                for warning in plan["warnings"]:
                    print(f"WARNING: {warning}")
            return 0
        if plan["route_count"] == 0 and not args.allow_empty:
            print(
                "ERROR: refusing to apply zero global routes without --allow-empty",
                file=sys.stderr,
            )
            return 2
        password = load_cudy_ssh_password(args.ssh_password)
        if not password:
            password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
        try:
            result = deploy_pbr_overrides(
                manifest,
                ssh_host=args.ssh_host,
                ssh_user=args.ssh_user,
                ssh_password=password,
                ssh_timeout=args.ssh_timeout,
                remote_dir=args.remote_dir,
                install_scripts=args.install_scripts,
                restart_pbr=not args.no_restart_pbr,
                prune_empty=args.prune_empty,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Applied PBR overrides to {args.ssh_host}")
            print(f"Backup: {result['backup_dir']}")
            if result["output"]:
                print(result["output"])
        return 0
    if args.command == "export-user-routes":
        manifest = export_user_routes(args.db, args.inventory, args.output_dir)
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            print(f"Exported user routes: {manifest['route_count']}")
            print(f"Groups: {manifest['group_count']}")
            print(f"Output: {manifest['output_dir']}")
            print(f"Routes file: {manifest['routes_file']}")
            for warning in manifest["warnings"]:
                print(f"WARNING: {warning}")
        return 0
    if args.command == "deploy-user-routes":
        manifest = export_user_routes(args.db, args.inventory, args.output_dir)
        plan = build_user_routes_deploy_plan(
            manifest,
            ssh_host=args.ssh_host,
            remote_dir=args.remote_dir,
            install_script=args.install_script,
            run_apply=not args.no_run_apply,
        )
        if not args.apply:
            payload = {"apply": False, "manifest": manifest, "plan": plan}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("Dry run. Use --apply to deploy to Cudy.")
                print(f"User routes: {plan['route_count']}")
                print(f"Groups: {plan['group_count']}")
                print(f"Remote: {args.ssh_user}@{args.ssh_host}:{args.remote_dir}")
                print(f"Uploads: {plan['upload_count']}")
                print(f"Install script: {bool(args.install_script)}")
                print(f"Run apply: {not args.no_run_apply}")
                for upload in plan["uploads"]:
                    print(f"  {upload['local']} -> {upload['remote']}")
                for warning in plan["warnings"]:
                    print(f"WARNING: {warning}")
            return 0
        if plan["route_count"] == 0 and not args.allow_empty:
            print(
                "ERROR: refusing to apply zero user routes without --allow-empty",
                file=sys.stderr,
            )
            return 2
        password = load_cudy_ssh_password(args.ssh_password)
        if not password:
            password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
        try:
            result = deploy_user_routes(
                manifest,
                ssh_host=args.ssh_host,
                ssh_user=args.ssh_user,
                ssh_password=password,
                ssh_timeout=args.ssh_timeout,
                remote_dir=args.remote_dir,
                install_script=args.install_script,
                run_apply=not args.no_run_apply,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Applied user routes to {args.ssh_host}")
            print(f"Backup: {result['backup_dir']}")
            if result["output"]:
                print(result["output"])
        return 0
    if args.command == "status-user-routes":
        password = load_cudy_ssh_password(args.ssh_password)
        if not password:
            password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
        try:
            status = collect_user_routes_status(
                ssh_host=args.ssh_host,
                ssh_user=args.ssh_user,
                ssh_password=password,
                ssh_timeout=args.ssh_timeout,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print("== routes.tsv ==")
            print(status["routes_tsv"] or "(missing)")
            print("\n== nft table ==")
            print(status["nft_table"] or "(missing)")
        return 0
    if args.command == "deploy-routes":
        pbr_manifest = export_pbr_overrides(args.db, args.inventory, args.pbr_output_dir)
        user_manifest = export_user_routes(args.db, args.inventory, args.user_output_dir)
        pbr_plan = build_pbr_deploy_plan(
            pbr_manifest,
            ssh_host=args.ssh_host,
            remote_dir=args.pbr_remote_dir,
            install_scripts=args.install_scripts,
            restart_pbr=not args.no_restart_pbr,
            prune_empty=args.prune_empty,
        )
        user_plan = build_user_routes_deploy_plan(
            user_manifest,
            ssh_host=args.ssh_host,
            remote_dir=args.user_remote_dir,
            install_script=args.install_scripts,
            run_apply=not args.no_run_user_apply,
        )
        should_apply_pbr = (
            pbr_plan["route_count"] > 0
            or args.prune_empty
            or args.install_scripts
            or args.allow_empty
        )
        should_apply_user = user_plan["route_count"] > 0 or args.allow_empty
        payload = {
            "apply": bool(args.apply),
            "pbr": {"manifest": pbr_manifest, "plan": pbr_plan, "will_apply": should_apply_pbr},
            "user_routes": {"manifest": user_manifest, "plan": user_plan, "will_apply": should_apply_user},
        }
        if not args.apply:
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("Dry run. Use --apply to deploy to Cudy.")
                print(f"Global routes: {pbr_plan['route_count']} apply={should_apply_pbr}")
                print(f"User routes: {user_plan['route_count']} apply={should_apply_user}")
                print(f"Remote: {args.ssh_user}@{args.ssh_host}")
                print(f"Install scripts: {bool(args.install_scripts)}")
                print(f"Restart PBR: {not args.no_restart_pbr}")
                print(f"Run user apply: {not args.no_run_user_apply}")
                print(f"PBR uploads: {pbr_plan['upload_count']}")
                print(f"User uploads: {user_plan['upload_count']}")
                text_warnings: list[str] = []
                if should_apply_pbr:
                    text_warnings.extend(pbr_plan["warnings"])
                if should_apply_user:
                    text_warnings.extend(
                        warning
                        for warning in user_plan["warnings"]
                        if "remote user-route apply script is not updated" not in warning
                    )
                if not args.install_scripts and should_apply_user:
                    text_warnings.append("use --install-scripts after changing OpenWrt scripts")
                for warning in text_warnings:
                    print(f"WARNING: {warning}")
            return 0
        if not should_apply_pbr and not should_apply_user:
            print("ERROR: refusing to apply zero routes without --allow-empty", file=sys.stderr)
            return 2
        password = load_cudy_ssh_password(args.ssh_password)
        if not password:
            password = getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
        results: dict[str, Any] = {"pbr": None, "user_routes": None}
        try:
            if should_apply_pbr:
                results["pbr"] = deploy_pbr_overrides(
                    pbr_manifest,
                    ssh_host=args.ssh_host,
                    ssh_user=args.ssh_user,
                    ssh_password=password,
                    ssh_timeout=args.ssh_timeout,
                    remote_dir=args.pbr_remote_dir,
                    install_scripts=args.install_scripts,
                    restart_pbr=not args.no_restart_pbr,
                    prune_empty=args.prune_empty,
                )
            if should_apply_user:
                results["user_routes"] = deploy_user_routes(
                    user_manifest,
                    ssh_host=args.ssh_host,
                    ssh_user=args.ssh_user,
                    ssh_password=password,
                    ssh_timeout=args.ssh_timeout,
                    remote_dir=args.user_remote_dir,
                    install_script=args.install_scripts,
                    run_apply=not args.no_run_user_apply,
                )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))
        else:
            print(f"Applied route deployment to {args.ssh_host}")
            if results["pbr"]:
                print(f"PBR backup: {results['pbr']['backup_dir']}")
            else:
                print("PBR layer skipped")
            if results["user_routes"]:
                print(f"User routes backup: {results['user_routes']['backup_dir']}")
                if results["user_routes"]["output"]:
                    print(results["user_routes"]["output"])
            else:
                print("User route layer skipped")
        return 0
    if args.command == "serve":
        app = App(args.db, args.inventory)
        server = Server((args.host, args.port), app)
        worker_stop = threading.Event()
        worker_thread: threading.Thread | None = None
        provider_thread: threading.Thread | None = None
        if not args.no_auto_worker:
            update_worker_status(
                "auto_probe",
                db_path=args.db,
                enabled=True,
                interval_seconds=max(30, args.auto_worker_interval),
                last_error=None,
            )
            try:
                update_worker_status("auto_probe", db_path=args.db, started=True)
                initial = create_auto_probe_jobs_once(
                    args.db,
                    args.inventory,
                    cache_ttl_seconds=args.auto_cache_ttl_seconds,
                    job_stale_seconds=args.auto_worker_job_stale_seconds,
                    agent_stale_seconds=args.auto_worker_agent_stale_seconds,
                    max_jobs=args.auto_worker_max_jobs,
                    max_candidates_per_job=args.auto_worker_max_candidates_per_job,
                    connect_timeout=args.auto_worker_connect_timeout,
                    max_time=args.auto_worker_max_time,
                    active_domain_limit=args.auto_worker_active_domain_limit,
                )
                update_worker_status(
                    "auto_probe",
                    db_path=args.db,
                    finished=True,
                    last_error=None,
                    last_result={
                        "initial": True,
                        "created": len(initial.get("created") or []),
                        "skipped": len(initial.get("skipped") or []),
                        "active_agents": initial.get("active_agents"),
                        "active_auto_domains": initial.get("active_auto_domains"),
                        "total_auto_domains": initial.get("total_auto_domains"),
                        "active_domain_limit": initial.get("active_domain_limit"),
                    },
                )
                if initial.get("created"):
                    print(f"auto-probe worker: created {len(initial['created'])} initial job(s)", file=sys.stderr)
            except Exception as exc:
                update_worker_status("auto_probe", db_path=args.db, finished=True, last_error=str(exc))
                print(f"auto-probe worker initial run failed: {exc}", file=sys.stderr)
            worker_thread = threading.Thread(
                target=auto_probe_worker_loop,
                kwargs={
                    "db_path": args.db,
                    "inventory_path": args.inventory,
                    "stop_event": worker_stop,
                    "interval_seconds": max(30, args.auto_worker_interval),
                    "cache_ttl_seconds": args.auto_cache_ttl_seconds,
                    "job_stale_seconds": args.auto_worker_job_stale_seconds,
                    "agent_stale_seconds": args.auto_worker_agent_stale_seconds,
                    "max_jobs": args.auto_worker_max_jobs,
                    "max_candidates_per_job": args.auto_worker_max_candidates_per_job,
                    "connect_timeout": args.auto_worker_connect_timeout,
                    "max_time": args.auto_worker_max_time,
                    "active_domain_limit": args.auto_worker_active_domain_limit,
                },
                daemon=True,
            )
            worker_thread.start()
        else:
            update_worker_status("auto_probe", db_path=args.db, enabled=False)
        if not args.no_provider_refresh_worker:
            update_worker_status(
                "provider_refresh",
                db_path=args.db,
                enabled=True,
                interval_seconds=max(60, args.provider_refresh_interval),
                provider=args.provider_refresh_provider,
                last_error=None,
            )
            provider_thread = threading.Thread(
                target=provider_refresh_worker_loop,
                kwargs={
                    "db_path": args.db,
                    "inventory_path": args.inventory,
                    "stop_event": worker_stop,
                    "interval_seconds": max(60, args.provider_refresh_interval),
                    "provider": args.provider_refresh_provider,
                    "skip_verify": args.provider_refresh_skip_verify,
                    "connect_timeout": args.provider_refresh_connect_timeout,
                    "max_time": args.provider_refresh_max_time,
                },
                daemon=True,
            )
            provider_thread.start()
        else:
            update_worker_status("provider_refresh", db_path=args.db, enabled=False)
        print(f"Serving on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")
        finally:
            worker_stop.set()
            server.server_close()
            if worker_thread:
                worker_thread.join(timeout=3)
            if provider_thread:
                provider_thread.join(timeout=3)
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
