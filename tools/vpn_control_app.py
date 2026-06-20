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
from datetime import datetime, timezone
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
]


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

CREATE TABLE IF NOT EXISTS service_aliases (
  alias TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  targets_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
    main { max-width: 1120px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section {
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
        <button id="saveDefault">Save</button>
      </div>
      <p id="defaultStatus" class="status"></p>
    </section>

    <section>
      <h2>Domain Routes</h2>
      <form id="routeForm" class="row">
        <input id="domainInput" type="text" placeholder="example.com" autocomplete="off">
        <select id="routeServer"></select>
        <button type="submit">Add</button>
      </form>
      <p id="routeStatus" class="status"></p>
      <table>
        <thead><tr><th>Domain</th><th>Server</th><th>Provider</th><th></th></tr></thead>
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
      <h2>Service Aliases</h2>
      <form id="aliasForm" class="row">
        <input id="aliasInput" type="text" placeholder="alias, e.g. телеграм" autocomplete="off">
        <input id="aliasLabel" type="text" placeholder="label" autocomplete="off">
        <input id="aliasTargets" type="text" placeholder="targets: domain, IP/CIDR, ..." autocomplete="off">
        <button type="submit">Save</button>
      </form>
      <p id="aliasStatus" class="status"></p>
      <table>
        <thead><tr><th>Alias</th><th>Label</th><th>Targets</th><th></th></tr></thead>
        <tbody id="aliasesBody"></tbody>
      </table>
    </section>
  </main>
  <script>
    const state = { servers: [], routes: [], user: null, aliases: [] };
    const serverLabel = id => (state.servers.find(s => s.id === id) || { label: id }).label;
    const serverProvider = id => (state.servers.find(s => s.id === id) || { provider: "" }).provider || "";

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
        return `<option value="${s.id}">${s.label} ${geo ? "(" + geo + ")" : ""}</option>`;
      }).join("");
      select.value = value || "auto";
    }

    function renderRoutes() {
      const body = document.getElementById("routesBody");
      if (!state.routes.length) {
        body.innerHTML = '<tr><td data-label="Domain" colspan="4" class="muted">No domain routes yet.</td></tr>';
        return;
      }
      body.innerHTML = state.routes.map(route => `
        <tr>
          <td data-label="Domain">${route.domain}</td>
          <td data-label="Server"><span class="pill">${serverLabel(route.server_id)}</span></td>
          <td data-label="Provider">${serverProvider(route.server_id)}</td>
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
          <td data-label="Alias">${item.alias}</td>
          <td data-label="Label">${item.label}</td>
          <td data-label="Targets">${(item.targets || []).join(", ")}</td>
          <td><button class="danger" data-delete-alias="${item.alias}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td data-label="Alias" colspan="4" class="muted">No aliases.</td></tr>';
      body.querySelectorAll("[data-delete-alias]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/service-aliases?alias=${encodeURIComponent(button.dataset.deleteAlias)}`, { method: "DELETE" });
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
      fillServerSelect(document.getElementById("defaultServer"), state.user.default_server_id);
      fillServerSelect(document.getElementById("routeServer"), "auto");
      renderRoutes();
      renderAliases();
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
          body: JSON.stringify({ server_id: document.getElementById("defaultServer").value })
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
            server_id: document.getElementById("routeServer").value
          })
        });
        document.getElementById("domainInput").value = "";
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
        await api("/api/service-aliases", {
          method: "POST",
          body: JSON.stringify({
            alias: document.getElementById("aliasInput").value,
            label: document.getElementById("aliasLabel").value,
            targets: document.getElementById("aliasTargets").value
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
    main { max-width: 1280px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
    h2 { font-size: 16px; margin: 0 0 14px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-weight: 600; }
    input[type="text"], input[type="password"], select {
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
  <main>
    <section>
      <h2>Servers</h2>
      <p id="serverStatus" class="status"></p>
      <table>
        <thead>
          <tr><th>ID</th><th>Label</th><th>Provider</th><th>Interface</th><th>Geo</th><th>Enabled</th><th>User</th><th></th></tr>
        </thead>
        <tbody id="serversBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Users</h2>
      <form id="newUserForm" class="toolbar">
        <div class="field"><label>ID</label><input id="newUserId" type="text" autocomplete="off"></div>
        <div class="field"><label>Name</label><input id="newUserName" type="text" autocomplete="off"></div>
        <div class="field"><label>Role</label><select id="newUserRole"><option value="user">user</option><option value="admin">admin</option></select></div>
        <div class="field"><label>Client IP</label><input id="newUserClientIp" type="text" placeholder="10.77.0.x" autocomplete="off"></div>
        <label class="inline muted"><input id="newUserCreateCudy" type="checkbox" checked> Create Cudy VPN .conf</label>
        <div class="field">
          <label>Password</label>
          <div class="inline">
            <input id="newUserPassword" type="password" autocomplete="new-password">
            <button class="secondary" type="button" data-toggle-password="newUserPassword">Show</button>
          </div>
        </div>
        <button type="submit">Create</button>
        <button id="syncCudyClients" class="secondary" type="button">Sync Cudy</button>
      </form>
      <p id="userStatus" class="status"></p>
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Role</th><th>Client IP</th><th>Default</th><th>Enabled</th><th>Login</th><th>Password</th><th>Actions</th></tr></thead>
        <tbody id="usersBody"></tbody>
      </table>
    </section>
    <section>
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
      <h3>Service Aliases</h3>
      <form id="adminAliasForm" class="toolbar">
        <div class="field"><label>Alias</label><input id="adminAliasInput" type="text" placeholder="телеграм" autocomplete="off"></div>
        <div class="field"><label>Label</label><input id="adminAliasLabel" type="text" placeholder="Telegram" autocomplete="off"></div>
        <div class="field"><label>Targets</label><input id="adminAliasTargets" type="text" placeholder="domain, IP/CIDR, ..." autocomplete="off"></div>
        <button type="submit">Save Alias</button>
      </form>
      <p id="adminAliasStatus" class="status"></p>
      <table>
        <thead><tr><th>Alias</th><th>Label</th><th>Targets</th><th></th></tr></thead>
        <tbody id="adminAliasesBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Global Domain Routes</h2>
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
    <section>
      <h2>Domain Routes</h2>
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
    <section>
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
    <section>
      <h2>Auto Probe Jobs</h2>
      <div class="toolbar">
        <button id="runAutoWorker" type="button">Run Worker Once</button>
        <button id="refreshAutoJobs" class="secondary" type="button">Refresh</button>
        <div class="field"><label>Max Jobs</label><input id="autoWorkerMaxJobs" type="number" min="1" max="50" step="1" value="5"></div>
        <div class="field"><label>Cache TTL sec</label><input id="autoWorkerCacheTtl" type="number" min="0" step="60" value="3600"></div>
      </div>
      <p id="autoProbeStatus" class="status"></p>
      <table>
        <thead><tr><th>Status</th><th>Domain</th><th>Candidates</th><th>Assigned</th><th>Claimed</th><th>Winner</th><th>Score</th><th>Updated</th></tr></thead>
        <tbody id="autoProbeJobsBody"></tbody>
      </table>
      <h3>Agents</h3>
      <table>
        <thead><tr><th>Device</th><th>User</th><th>Platform</th><th>Last Seen</th><th>Reported</th><th>Health</th><th>Applied</th><th>Errors</th></tr></thead>
        <tbody id="agentStatusBody"></tbody>
      </table>
    </section>
    <section>
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
    <section>
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
    const state = { servers: [], users: [], routes: [], globalRoutes: [], autoCache: [], autoCandidates: [], probeJobs: [], agentStatus: [], transportConfigs: [], serviceAliases: [] };
    const ALL_REST = "__all_rest__";
    const autoEditors = { globalDefault: [], userDefault: [], globalRoute: [], adminRoute: [] };
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
      return state.servers.map(s => `<option value="${s.id}" ${s.id === value ? "selected" : ""}>${s.label}</option>`).join("");
    }
    function physicalServerOptions(value) {
      return state.servers
        .filter(s => s.id !== "auto" && s.enabled && s.user_visible)
        .map(s => `<option value="${s.id}" ${s.id === value ? "selected" : ""}>${s.label}</option>`)
        .join("");
    }
    function physicalServers() {
      return state.servers.filter(s => s.id !== "auto" && s.enabled && s.user_visible);
    }
    function userOptions(value) {
      return state.users.map(u => `<option value="${u.id}" ${u.id === value ? "selected" : ""}>${u.id}</option>`).join("");
    }
    function autoCandidateUserOptions(value) {
      return `<option value="" ${!value ? "selected" : ""}>Global</option>` + userOptions(value);
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
      autoEditors[prefix] = [...(serverIds || [])];
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
      if (!winners.length) {
        container.textContent = "";
        return;
      }
      container.innerHTML = "Last winners: " + winners.map(item => {
        const latency = item.latency_ms == null ? "-" : `${item.latency_ms}ms`;
        const speed = item.speed_mbps == null ? "" : `, ${item.speed_mbps}Mbps`;
        const target = item.domain || "";
        return `${item.winner_server_id} (${latency}${speed}, ${target})`;
      }).join(" | ");
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
    function expandAutoCandidates(values) {
      const result = [];
      for (const value of values) {
        if (!value) break;
        if (value === ALL_REST) {
          for (const server of physicalServers()) {
            if (!result.includes(server.id)) result.push(server.id);
          }
          break;
        }
        if (!result.includes(value)) result.push(value);
      }
      return result;
    }
    function selectedAutoCandidates(prefix) {
      return expandAutoCandidates(autoEditors[prefix] || []);
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
      body.innerHTML = state.users.map(u => `
        <tr data-id="${u.id}">
          <td>${u.id}</td>
          <td><input type="text" data-field="display_name" value="${u.display_name}"></td>
          <td><select data-field="role"><option value="user" ${u.role === "user" ? "selected" : ""}>user</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>admin</option></select></td>
          <td><input type="text" data-field="client_ip" value="${u.client_ip || ""}" placeholder="10.77.0.x"></td>
          <td><select data-field="default_server_id">${serverOptions(u.default_server_id)}</select></td>
          <td><input type="checkbox" data-field="enabled" ${u.enabled ? "checked" : ""}></td>
          <td>${u.has_login ? "yes" : "no"}</td>
          <td class="inline">
            <input type="password" data-field="password" data-password-input="${u.id}" placeholder="new password">
            <button class="secondary" type="button" data-toggle-row-password="${u.id}">Show</button>
            <button class="secondary" data-password="${u.id}">Set</button>
          </td>
          <td class="inline">
            <button data-save-user="${u.id}">Save</button>
            <a class="button secondary" href="/api/admin/client-config?user_id=${encodeURIComponent(u.id)}">Config</a>
            <button class="danger" data-delete-user="${u.id}">Delete</button>
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
          button.textContent = visible ? "Show" : "Hide";
        });
      });
      body.querySelectorAll("[data-delete-user]").forEach(button => {
        button.addEventListener("click", async () => {
          const userId = button.dataset.deleteUser;
          const revoke = confirm(`Delete ${userId} and revoke its Cudy VPN peer? Press Cancel to delete only locally.`);
          if (!revoke && !confirm(`Delete ${userId} only from local control panel?`)) return;
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
          <td><button class="danger" data-delete-alias="${item.alias}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="4" class="muted">No aliases.</td></tr>';
      body.querySelectorAll("[data-delete-alias]").forEach(button => {
        button.addEventListener("click", async () => {
          await api(`/api/service-aliases?alias=${encodeURIComponent(button.dataset.deleteAlias)}`, { method: "DELETE" });
          await load();
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
      body.innerHTML = state.agentStatus.length ? state.agentStatus.map(item => {
        const health = (item.status || {}).health || {};
        const errors = ((item.status || {}).errors || []).concat((item.status || {}).status_errors || []);
        return `
          <tr>
            <td>${item.device_id}</td>
            <td>${item.user_id}</td>
            <td>${item.platform || ""}</td>
            <td>${item.last_seen_at || ""}</td>
            <td>${item.reported_at || ""}</td>
            <td>${health.ok === true ? "ok" : health.ok === false ? "fail" : ""}</td>
            <td>${health.applied ?? ""}</td>
            <td>${errors.length ? errors.slice(0, 2).join("; ") : ""}</td>
          </tr>
        `;
      }).join("") : '<tr><td colspan="8" class="muted">No agent status.</td></tr>';
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
    async function load() {
      const data = await api("/api/admin");
      state.servers = data.servers;
      state.users = data.users;
      state.routes = data.routes;
      state.globalRoutes = data.global_routes || [];
      state.autoCache = data.auto_cache || [];
      state.autoCandidates = data.auto_candidates || [];
      state.probeJobs = data.probe_jobs || [];
      state.agentStatus = data.agent_status || [];
      state.transportConfigs = data.transport_configs || [];
      state.serviceAliases = data.service_aliases || [];
      renderServers();
      renderUsers();
      renderServiceAliases();
      renderGlobalRoutes();
      renderRoutes();
      renderAutoCache();
      renderAutoProbeJobs();
      renderAgentStatus();
      renderProviderTransports();
      document.getElementById("adminRouteServer").innerHTML = serverOptions(document.getElementById("adminRouteServer").value || "auto");
      document.getElementById("globalRouteServer").innerHTML = serverOptions(document.getElementById("globalRouteServer").value || "auto");
      document.getElementById("autoCacheServer").innerHTML = physicalServerOptions(document.getElementById("autoCacheServer").value);
      document.getElementById("autoSelectUser").innerHTML = autoCandidateUserOptions(document.getElementById("autoSelectUser").value);
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
        status.innerHTML = result.config_download_url
          ? `User created. <a href="${result.config_download_url}">Download .conf</a>`
          : "User created.";
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
    document.querySelectorAll("[data-toggle-password]").forEach(button => {
      button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.togglePassword);
        const visible = input.type === "text";
        input.type = visible ? "password" : "text";
        button.textContent = visible ? "Show" : "Hide";
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


def init_db(db_path: Path, inventory_path: Path, *, reset_from_inventory: bool = False) -> None:
    inventory = load_inventory(inventory_path)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        seed_inventory(conn, inventory, reset_from_inventory=reset_from_inventory)
        seed_service_aliases(conn)
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


def hash_device_token(token: str, salt_b64: str | None = None) -> tuple[str, str]:
    return hash_password(token, salt_b64)


def verify_device_token(token: str, salt_b64: str | None, hash_b64: str | None) -> bool:
    if not token or not salt_b64 or not hash_b64:
        return False
    _, expected = hash_device_token(token, salt_b64)
    return hmac.compare_digest(expected, hash_b64)


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


def user_servers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        """
        SELECT id, label, provider, kind, interface, geo_country, geo_region, enabled, user_visible
        FROM servers
        WHERE enabled = 1 AND user_visible = 1
        ORDER BY sort_order, label
        """,
    )


def admin_servers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        """
        SELECT id, label, provider, kind, interface, geo_country, geo_region, endpoint,
               switch_command, enabled, user_visible, admin_visible, sort_order
        FROM servers
        WHERE admin_visible = 1 OR id = 'auto'
        ORDER BY sort_order, label
        """,
    )


def server_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
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
    }


def normalize_transport_type(value: str) -> str:
    transport_type = (value or "").strip()
    allowed = {"http-proxy-tun", "vless-reality-tun", "sing-box-json"}
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
               s.label, s.provider, s.kind
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
                "source": item.get("source") or "",
                "version": item.get("version") or "",
                "expires_at": item.get("expires_at"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "endpoint": transport_endpoint_summary(item.get("config") or {}),
            }
        )
    return result


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

LOKVPN_PROFILE_MAP: dict[str, tuple[int, int]] = {
    "smart1": (0, 1),
    "de1": (1, 0),
    "ru1": (2, 0),
    "nl1": (3, 0),
    "fr1": (4, 0),
    "se1": (5, 0),
    "smart2": (6, 1),
    "de2": (7, 0),
    "ru2": (8, 0),
    "nl2": (9, 0),
    "fr2": (10, 0),
    "se2": (11, 0),
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
    if profile not in LOKVPN_PROFILE_MAP:
        raise ValueError(f"Unknown LokVPN profile server_id: {server_id}")
    return profile


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
    idx, outbound_idx = LOKVPN_PROFILE_MAP[profile]
    try:
        outbound = subscription[idx]["outbounds"][outbound_idx]
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
    if any(not parsed[key] for key in ("server", "server_port", "uuid", "flow", "sni", "public_key", "short_id")):
        raise ValueError(f"Could not parse LokVPN profile {profile}")
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
    if not sub_url:
        return {
            "provider": "lokvpn",
            "refreshed": [],
            "failed": [{"server_id": server_id, "error": "LokVPN subscription URL is not configured"} for server_id in selected_ids],
        }
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
        if item == "auto":
            raise ValueError("Auto candidate list must contain real servers, not auto")
        if item not in seen:
            result.append(item)
            seen.add(item)
    if not result:
        raise ValueError("candidate list cannot be empty")
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
    for entry in entries:
        try:
            entry["candidate_server_ids"] = json.loads(entry["candidate_server_ids"])
        except json.JSONDecodeError:
            entry["candidate_server_ids"] = []
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

    cached = auto_cache.get(domain)
    if not cached or not cached.get("selected_server_id"):
        if auto_policy:
            candidates = ", ".join(auto_policy.get("candidate_server_ids") or [])
            warnings.append(
                f"{context}: Auto has no cached selected server for {domain}; "
                f"candidate policy {auto_policy['scope']}=[{candidates}]"
            )
        else:
            warnings.append(f"{context}: Auto has no cached selected server for {domain}; no candidate policy")
        return None, cached

    selected_server_id = str(cached["selected_server_id"])
    if selected_server_id == "auto":
        warnings.append(f"{context}: Auto cache for {domain} points back to auto")
        return None, cached
    if selected_server_id not in servers:
        warnings.append(f"{context}: Auto cache for {domain} points to unknown server {selected_server_id}")
        return None, cached

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
    return probe_url_from_note(note) or default_probe_url_for_cidr(target_cidr)


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
        if assigned_device_id and row(conn, "SELECT id FROM agent_devices WHERE id = ?", (assigned_device_id,)) is None:
            raise ValueError(f"Unknown agent device: {assigned_device_id}")
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
        entries = rows(
            conn,
            """
            SELECT *
            FROM agent_probe_jobs
            WHERE status = 'pending'
              AND (assigned_device_id = '' OR assigned_device_id = ?)
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (device["id"], max_limit),
        )
        claimed: list[dict[str, Any]] = []
        for item in entries:
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
                    claimed.append(probe_job_row_to_dict(saved))
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


def agent_can_probe(agent: dict[str, Any]) -> bool:
    status = agent.get("status") or {}
    capabilities = status.get("capabilities") if isinstance(status.get("capabilities"), dict) else {}
    if "can_probe" in capabilities:
        return bool(capabilities.get("can_probe"))
    platform_name = str(status.get("platform") or agent.get("platform") or "").lower()
    if platform_name == "android":
        return False
    return True


def choose_probe_agent(
    agents: list[dict[str, Any]],
    *,
    domain: str,
    user_id: str,
) -> str:
    probe_agents = [agent for agent in agents if agent_can_probe(agent)]
    for agent in probe_agents:
        if agent_reports_domain(agent, domain=domain, user_id=user_id):
            return str(agent["device_id"])
    if user_id:
        for agent in probe_agents:
            if agent.get("user_id") == user_id:
                return str(agent["device_id"])
    return ""


def auto_probe_domain_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
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
        result.append(entry)
    return result


def create_auto_probe_jobs_once(
    db_path: Path,
    inventory_path: Path,
    *,
    cache_ttl_seconds: int = 3600,
    job_stale_seconds: int = 900,
    agent_stale_seconds: int = 600,
    max_jobs: int = 5,
    connect_timeout: int = 5,
    max_time: int = 12,
) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    reference = datetime.now(timezone.utc)
    stale_started_before = (reference.replace(microsecond=0).timestamp() - job_stale_seconds)
    stale_cutoff = datetime.fromtimestamp(stale_started_before, timezone.utc).replace(microsecond=0).isoformat()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    job_requests: list[dict[str, Any]] = []
    active_agent_count = 0
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
        cache = auto_cache_map(conn)
        for spec in auto_probe_domain_rows(conn):
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
            policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=domain)
            candidates = list((policy or {}).get("candidate_server_ids") or default_candidates)
            candidates = [server_id for server_id in candidates if server_id in servers and servers[server_id].get("enabled") and servers[server_id].get("user_visible")]
            if not candidates:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "no_candidates"})
                continue
            assigned_device_id = choose_probe_agent(agents, domain=domain, user_id=user_id)
            if not assigned_device_id and not agents:
                skipped.append({"domain": domain, "user_id": user_id, "reason": "no_active_agent"})
                continue
            job_requests.append(
                {
                    "domain": domain,
                    "url": spec.get("url") or None,
                    "candidate_server_ids": candidates,
                    "user_id": user_id,
                    "assigned_device_id": assigned_device_id,
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
    }


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
    connect_timeout: int,
    max_time: int,
) -> None:
    while not stop_event.wait(interval_seconds):
        try:
            result = create_auto_probe_jobs_once(
                db_path,
                inventory_path,
                cache_ttl_seconds=cache_ttl_seconds,
                job_stale_seconds=job_stale_seconds,
                agent_stale_seconds=agent_stale_seconds,
                max_jobs=max_jobs,
                connect_timeout=connect_timeout,
                max_time=max_time,
            )
            created_count = len(result.get("created") or [])
            if created_count:
                print(f"auto-probe worker: created {created_count} job(s)", file=sys.stderr)
        except Exception as exc:
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
    while not stop_event.wait(interval_seconds):
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
            if refreshed or failed:
                print(f"provider-refresh worker: refreshed={refreshed} failed={failed}", file=sys.stderr)
        except Exception as exc:
            print(f"provider-refresh worker failed: {exc}", file=sys.stderr)


def server_metadata(server: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(server.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        return {}


def default_auto_candidate_ids(servers: dict[str, dict[str, Any]]) -> list[str]:
    return [
        server_id
        for server_id, server in sorted(servers.items(), key=lambda item: (item[1].get("sort_order") or 0, item[0]))
        if server_id != "auto" and server.get("enabled") and server.get("user_visible")
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
        "out=$(curl -4 -L -sS -o /dev/null "
        f"--interface {shlex.quote(iface)} "
        f"--connect-timeout {int(connect_timeout)} --max-time {int(max_time)} "
        "-w 'http_code=%{http_code}\\ntime_total=%{time_total}\\nremote_ip=%{remote_ip}\\n"
        "size_download=%{size_download}\\nspeed_download=%{speed_download}\\n' "
        f"{shlex.quote(url)} 2>&1); "
        "rc=$?; printf 'rc=%s\\n%s\\n' \"$rc\" \"$out\""
    )
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    stdout.channel.recv_exit_status()
    parsed = parse_curl_probe_output(out + ("\n" + err if err.strip() else ""))
    parsed["raw"] = (out + err).strip()
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
            ok = probe.get("rc") == 0 and 200 <= http_code < 500
            check.update(
                {
                    "ok": ok,
                    "status": "ok" if ok else "failed",
                    "http_code": http_code,
                    "score_ms": probe.get("time_total_ms"),
                    "remote_ip": probe.get("remote_ip"),
                    "curl_rc": probe.get("rc"),
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
        auto_policy = (
            resolve_auto_candidate_policy(conn, user_id=user_id, domain=route["domain"])
            if requested_server_id == "auto"
            else None
        )
        resolved_server_id, cached = resolve_route_server(
            domain=route["domain"],
            requested_server_id=requested_server_id,
            servers=servers,
            auto_cache=cached_auto,
            auto_policy=auto_policy,
            context=f"{user_id}/{route['domain']}",
            warnings=route_warnings,
        )
        warnings.extend(route_warnings)
        server_id = resolved_server_id or requested_server_id
        referenced_server_ids.add(server_id)
        domain_routes.append(
            {
                "domain": route["domain"],
                "source": route["source"],
                "requested_server_id": requested_server_id,
                "server_id": server_id,
                "resolved_server_id": resolved_server_id,
                "server": compact_server(servers.get(server_id)),
                "auto_cache": cached,
                "auto_candidate_policy": auto_policy,
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
        if requested_server_id == "auto" and target_type == "domain":
            with connect(db_path) as conn:
                auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=domain)
        elif requested_server_id == "auto":
            warnings.append(f"{user_id}/{label}: IP/CIDR route cannot use auto; skipped")
            continue
        resolved_server_id, cached = resolve_route_server(
            domain=domain or target,
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
            commands.append("/etc/init.d/pbr restart")
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
        try:
            entry["targets"] = json.loads(entry.pop("targets_json") or "[]")
        except json.JSONDecodeError:
            entry["targets"] = []
    return entries


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
    cache_key = ""
    if kind == "ip":
        cache_key = auto_cache_key_for_ip_route((rule or {}).get("target_cidr") or target)
    else:
        cache_key = target
    if rule:
        requested_server_id = str(rule["server_id"])
        auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=cache_key) if requested_server_id == "auto" else None
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
        alias = row(conn, "SELECT alias, label, targets_json, updated_at FROM service_aliases WHERE alias = ?", (alias_key,)) if alias_key else None
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
            alias_info = {"alias": alias["alias"], "label": alias["label"], "targets": raw_targets}
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
    result: list[dict[str, Any]] = []
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
    return {"ok": True, "target": target, "cache_keys": keys, "winners": result}


class App:
    def __init__(self, db_path: Path, inventory_path: Path):
        self.db_path = db_path
        self.inventory_path = inventory_path
        self.agent_token_cache: dict[str, tuple[float, dict[str, Any]]] = {}
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

    def cache_agent(self, token: str, device: dict[str, Any]) -> None:
        with self.agent_token_cache_lock:
            self.agent_token_cache[token] = (time.time() + AGENT_TOKEN_CACHE_SECONDS, dict(device))


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

    def send_json(
        self,
        data: Any,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, error: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"error": error}, status)

    def send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("location", location)
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

    def require_agent(self) -> dict[str, Any]:
        token = self.agent_token()
        if not token:
            raise PermissionError("Agent token required")
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
            elif parsed.path == "/api/agent/config":
                device = self.require_agent()
                with self.app.conn() as conn:
                    self.send_json(build_agent_config(conn, user_id=device["user_id"], device=device))
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
            else:
                self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
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
                self.require_user()
                self.send_json(
                    save_service_alias(
                        self.app.db_path,
                        self.app.inventory_path,
                        alias=str(data.get("alias") or ""),
                        label=str(data.get("label") or ""),
                        targets=data.get("targets") or "",
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
            elif parsed.path == "/api/agent/status":
                device = self.require_agent()
                self.send_json(self.api_agent_status(device, data))
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
            elif parsed.path == "/api/service-aliases":
                self.require_user()
                query = parse_qs(parsed.query)
                self.send_json(delete_service_alias(self.app.db_path, self.app.inventory_path, alias=query.get("alias", [""])[0]))
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
            return {"user": user, "servers": user_servers(conn), "routes": routes, "aliases": service_alias_rows(conn)}

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
                "agent_status": agent_status,
            }

    def api_set_default_server(self, data: dict[str, Any]) -> dict[str, Any]:
        user_id = self.require_user()["id"]
        server_id = str(data.get("server_id") or "")
        timestamp = now()
        with self.app.conn() as conn:
            validate_server_id(conn, server_id, require_user_visible=True)
            cursor = conn.execute(
                "UPDATE users SET default_server_id = ?, updated_at = ? WHERE id = ?",
                (server_id, timestamp, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown user: {user_id}")
        return {"ok": True}

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
            result["config_download_url"] = cudy_client["config_download_url"]
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
            connect_timeout=max(1, min(int(data.get("connect_timeout") or 5), 60)),
            max_time=max(1, min(int(data.get("max_time") or 12), 120)),
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
        domain = normalize_domain(str(data.get("domain") or ""))
        server_id = str(data.get("server_id") or "")
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
        return {"ok": True, "domain": domain, "server_id": server_id}

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
    print(f"Auto candidate lists: {auto_candidate_count}")
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

    create_user_parser = sub.add_parser("create-user", help="Create or update a login user.")
    create_user_parser.add_argument("user_id")
    create_user_parser.add_argument("--display-name")
    create_user_parser.add_argument("--role", choices=["admin", "user"], default="user")
    create_user_parser.add_argument("--client-ip")
    create_user_parser.add_argument("--password", help="Prefer interactive prompt or env in normal use.")
    create_user_parser.add_argument("--disabled", action="store_true")
    create_user_parser.add_argument("--no-password-change", action="store_true")

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
    transport_json_parser.add_argument("transport_type", choices=["vless-reality-tun", "sing-box-json", "http-proxy-tun"])
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

    auto_cache_list_parser = sub.add_parser("auto-cache-list", help="List cached Auto domain choices.")
    auto_cache_list_parser.add_argument("--json", action="store_true", help="Print JSON cache entries.")

    auto_cache_set_parser = sub.add_parser("auto-cache-set", help="Set cached Auto choice for one domain.")
    auto_cache_set_parser.add_argument("domain")
    auto_cache_set_parser.add_argument("selected_server_id")
    auto_cache_set_parser.add_argument("--score-ms", type=int)
    auto_cache_set_parser.add_argument("--status", default="manual")

    auto_cache_delete_parser = sub.add_parser("auto-cache-delete", help="Delete cached Auto choice for one domain.")
    auto_cache_delete_parser.add_argument("domain")

    auto_candidates_list_parser = sub.add_parser("auto-candidates-list", help="List Auto candidate server policies.")
    auto_candidates_list_parser.add_argument("--json", action="store_true", help="Print JSON policies.")

    auto_candidates_set_parser = sub.add_parser("auto-candidates-set", help="Set Auto candidate server list.")
    auto_candidates_set_parser.add_argument("candidate_server_ids", help="Comma/space-separated server ids in priority order.")
    auto_candidates_set_parser.add_argument("--user-id", default="", help="Blank means global policy.")
    auto_candidates_set_parser.add_argument("--domain", default="", help="Blank means default policy for all domains.")
    auto_candidates_set_parser.add_argument("--disabled", action="store_true")

    auto_candidates_delete_parser = sub.add_parser("auto-candidates-delete", help="Delete Auto candidate server list.")
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
    auto_worker_once_parser.add_argument("--connect-timeout", type=int, default=5)
    auto_worker_once_parser.add_argument("--max-time", type=int, default=12)
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
    serve_parser.add_argument("--auto-worker-connect-timeout", type=int, default=5)
    serve_parser.add_argument("--auto-worker-max-time", type=int, default=12)
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
                print("Auto candidate policies are empty.")
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
            f"Auto candidates saved: {item['scope']} user={item['user_id'] or '-'} "
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
        print(f"Auto candidates deleted: {item['scope']} user={item['user_id'] or '-'} domain={item['domain'] or '*'}")
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
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
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
            try:
                initial = create_auto_probe_jobs_once(
                    args.db,
                    args.inventory,
                    cache_ttl_seconds=args.auto_cache_ttl_seconds,
                    job_stale_seconds=args.auto_worker_job_stale_seconds,
                    agent_stale_seconds=args.auto_worker_agent_stale_seconds,
                    max_jobs=args.auto_worker_max_jobs,
                    connect_timeout=args.auto_worker_connect_timeout,
                    max_time=args.auto_worker_max_time,
                )
                if initial.get("created"):
                    print(f"auto-probe worker: created {len(initial['created'])} initial job(s)", file=sys.stderr)
            except Exception as exc:
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
                    "connect_timeout": args.auto_worker_connect_timeout,
                    "max_time": args.auto_worker_max_time,
                },
                daemon=True,
            )
            worker_thread.start()
        if not args.no_provider_refresh_worker:
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
