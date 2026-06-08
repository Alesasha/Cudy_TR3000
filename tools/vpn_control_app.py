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
import json
import os
import re
import secrets
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vpn_inventory.json"
DEFAULT_DB = ROOT / "data" / "vpn_control.db"
DEFAULT_USER_ID = "default"
DEFAULT_PBR_EXPORT_DIR = ROOT / "build" / "pbr-overrides"
DEFAULT_USER_ROUTES_EXPORT_DIR = ROOT / "build" / "user-routes"
DEFAULT_CUDY_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"
DEFAULT_CUDY_HOST = "192.168.8.1"
DEFAULT_CUDY_USER = "root"
REMOTE_PBR_DIR = "/etc/pbr-overrides"
REMOTE_USER_ROUTES_DIR = "/etc/cudy-user-routes"
REMOTE_PBR_SCRIPT = "/usr/share/pbr/pbr.user.opencck-merged-vpn"
LOCAL_PBR_SCRIPT = ROOT / "openwrt" / "pbr.user.opencck-merged-vpn"
LOCAL_SWITCHER_INSTALLER = ROOT / "openwrt" / "install-vpn-switchers.sh"
LOCAL_USER_ROUTES_APPLY = ROOT / "openwrt" / "cudy-user-routes-apply"
SESSION_COOKIE = "vpn_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 210_000
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
SAFE_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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

CREATE TABLE IF NOT EXISTS global_domain_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT NOT NULL UNIQUE,
  server_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
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
  </main>
  <script>
    const state = { servers: [], routes: [], user: null };
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
          await api(`/api/domain-routes?user_id=default&domain=${encodeURIComponent(button.dataset.delete)}`, { method: "DELETE" });
          await load();
        });
      });
    }

    async function load() {
      const data = await api("/api/bootstrap?user_id=default");
      state.servers = data.servers;
      state.routes = data.routes;
      state.user = data.user;
      fillServerSelect(document.getElementById("defaultServer"), state.user.default_server_id);
      fillServerSelect(document.getElementById("routeServer"), "auto");
      renderRoutes();
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
          body: JSON.stringify({ user_id: "default", server_id: document.getElementById("defaultServer").value })
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
            user_id: "default",
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
        <div class="field">
          <label>Password</label>
          <div class="inline">
            <input id="newUserPassword" type="password" autocomplete="new-password">
            <button class="secondary" type="button" data-toggle-password="newUserPassword">Show</button>
          </div>
        </div>
        <button type="submit">Create</button>
      </form>
      <p id="userStatus" class="status"></p>
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Role</th><th>Client IP</th><th>Default</th><th>Enabled</th><th>Login</th><th>Password</th><th></th></tr></thead>
        <tbody id="usersBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Global Domain Routes</h2>
      <form id="globalRouteForm" class="toolbar">
        <div class="field"><label>Domain</label><input id="globalRouteDomain" type="text" placeholder="example.com" autocomplete="off"></div>
        <div class="field"><label>Server</label><select id="globalRouteServer"></select></div>
        <button type="submit">Save Global</button>
      </form>
      <p id="globalRouteStatus" class="status"></p>
      <table>
        <thead><tr><th>Domain</th><th>Server</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="globalRoutesBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Domain Routes</h2>
      <form id="adminRouteForm" class="toolbar">
        <div class="field"><label>User</label><select id="routeUser"></select></div>
        <div class="field"><label>Domain</label><input id="adminRouteDomain" type="text" placeholder="example.com" autocomplete="off"></div>
        <div class="field"><label>Server</label><select id="adminRouteServer"></select></div>
        <button type="submit">Save Route</button>
      </form>
      <p id="adminRouteStatus" class="status"></p>
      <table>
        <thead><tr><th>User</th><th>Domain</th><th>Server</th><th>Enabled</th><th></th></tr></thead>
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
      <table>
        <thead><tr><th>Domain</th><th>Selected Server</th><th>Score</th><th>Status</th><th>Checked</th><th></th></tr></thead>
        <tbody id="autoCacheBody"></tbody>
      </table>
    </section>
    <section>
      <h2>Auto Candidate Lists</h2>
      <form id="autoCandidateForm" class="toolbar">
        <div class="field"><label>User</label><select id="autoCandidateUser"></select></div>
        <div class="field"><label>Domain</label><input id="autoCandidateDomain" type="text" placeholder="blank = all domains" autocomplete="off"></div>
        <div class="field"><label>Servers</label><input id="autoCandidateServers" type="text" placeholder="proxyde, proxyus, uswest" autocomplete="off"></div>
        <button type="submit">Save List</button>
      </form>
      <p id="autoCandidateStatus" class="status"></p>
      <table>
        <thead><tr><th>Scope</th><th>User</th><th>Domain</th><th>Servers</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="autoCandidatesBody"></tbody>
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
    const state = { servers: [], users: [], routes: [], globalRoutes: [], autoCache: [], autoCandidates: [] };
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
    function userOptions(value) {
      return state.users.map(u => `<option value="${u.id}" ${u.id === value ? "selected" : ""}>${u.id}</option>`).join("");
    }
    function autoCandidateUserOptions(value) {
      return `<option value="" ${!value ? "selected" : ""}>Global</option>` + userOptions(value);
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
          <td><button data-save-user="${u.id}">Save</button></td>
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
      document.getElementById("routeUser").innerHTML = userOptions(document.getElementById("routeUser").value);
    }
    function renderGlobalRoutes() {
      const body = document.getElementById("globalRoutesBody");
      body.innerHTML = state.globalRoutes.length ? state.globalRoutes.map(r => `
        <tr>
          <td>${r.domain}</td>
          <td>${r.server_id}</td>
          <td>${r.enabled ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-global-route="${r.domain}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="4" class="muted">No global routes.</td></tr>';
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
          <td>${r.enabled ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-route="${r.user_id}|${r.domain}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="4" class="muted">No routes.</td></tr>';
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
    function renderAutoCandidates() {
      const body = document.getElementById("autoCandidatesBody");
      body.innerHTML = state.autoCandidates.length ? state.autoCandidates.map(r => `
        <tr>
          <td>${r.scope}</td>
          <td>${r.user_id || "Global"}</td>
          <td>${r.domain || "All"}</td>
          <td>${(r.candidate_server_ids || []).join(", ")}</td>
          <td>${r.enabled ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-auto-candidate="${r.user_id || ""}|${r.domain || ""}">Delete</button></td>
        </tr>
      `).join("") : '<tr><td colspan="6" class="muted">No Auto candidate lists.</td></tr>';
      body.querySelectorAll("[data-delete-auto-candidate]").forEach(button => {
        button.addEventListener("click", async () => {
          const [userId, domain] = button.dataset.deleteAutoCandidate.split("|");
          await api(`/api/admin/auto-candidates?user_id=${encodeURIComponent(userId)}&domain=${encodeURIComponent(domain)}`, { method: "DELETE" });
          await load();
        });
      });
    }
    async function load() {
      const data = await api("/api/admin");
      state.servers = data.servers;
      state.users = data.users;
      state.routes = data.routes;
      state.globalRoutes = data.global_routes || [];
      state.autoCache = data.auto_cache || [];
      state.autoCandidates = data.auto_candidates || [];
      renderServers();
      renderUsers();
      renderGlobalRoutes();
      renderRoutes();
      renderAutoCache();
      renderAutoCandidates();
      document.getElementById("adminRouteServer").innerHTML = serverOptions(document.getElementById("adminRouteServer").value || "auto");
      document.getElementById("globalRouteServer").innerHTML = serverOptions(document.getElementById("globalRouteServer").value || "auto");
      document.getElementById("autoCacheServer").innerHTML = physicalServerOptions(document.getElementById("autoCacheServer").value);
      document.getElementById("autoCandidateUser").innerHTML = autoCandidateUserOptions(document.getElementById("autoCandidateUser").value);
      document.getElementById("routeUser").innerHTML = userOptions(document.getElementById("routeUser").value);
    }
    document.getElementById("newUserForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("userStatus");
      status.className = "status";
      try {
        await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({
            id: document.getElementById("newUserId").value,
            display_name: document.getElementById("newUserName").value || document.getElementById("newUserId").value,
            role: document.getElementById("newUserRole").value,
            client_ip: document.getElementById("newUserClientIp").value,
            default_server_id: "auto",
            enabled: true,
            password: document.getElementById("newUserPassword").value
          })
        });
        event.target.reset();
        status.textContent = "User created.";
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
            server_id: document.getElementById("globalRouteServer").value
          })
        });
        document.getElementById("globalRouteDomain").value = "";
        status.textContent = "Global route saved.";
        status.className = "status ok";
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
            server_id: document.getElementById("adminRouteServer").value
          })
        });
        document.getElementById("adminRouteDomain").value = "";
        status.textContent = "Route saved.";
        status.className = "status ok";
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
    document.getElementById("autoCandidateForm").addEventListener("submit", async event => {
      event.preventDefault();
      const status = document.getElementById("autoCandidateStatus");
      status.className = "status";
      try {
        await api("/api/admin/auto-candidates", {
          method: "POST",
          body: JSON.stringify({
            user_id: document.getElementById("autoCandidateUser").value,
            domain: document.getElementById("autoCandidateDomain").value,
            candidate_server_ids: document.getElementById("autoCandidateServers").value
          })
        });
        document.getElementById("autoCandidateDomain").value = "";
        document.getElementById("autoCandidateServers").value = "";
        status.textContent = "Auto candidate list saved.";
        status.className = "status ok";
        await load();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
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


def load_inventory(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def init_db(db_path: Path, inventory_path: Path, *, reset_from_inventory: bool = False) -> None:
    inventory = load_inventory(inventory_path)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        seed_inventory(conn, inventory, reset_from_inventory=reset_from_inventory)
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
                   metadata_json
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
        ("", domain),
        (user_id, ""),
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


def save_auto_cache_entry(
    db_path: Path,
    inventory_path: Path,
    *,
    domain: str,
    selected_server_id: str,
    score_ms: int | None,
    status: str,
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
    with connect(db_path) as conn:
        validate_server_id(conn, selected_server_id, require_user_visible=True)
        conn.execute(
            """
            INSERT INTO domain_auto_cache (
              domain, selected_server_id, score_ms, status, checked_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, '{}')
            ON CONFLICT(domain)
            DO UPDATE SET selected_server_id = excluded.selected_server_id,
                          score_ms = excluded.score_ms,
                          status = excluded.status,
                          checked_at = excluded.checked_at
            """,
            (domain, selected_server_id, score_ms, status, timestamp),
        )
    return {
        "ok": True,
        "domain": domain,
        "selected_server_id": selected_server_id,
        "score_ms": score_ms,
        "status": status,
        "checked_at": timestamp,
    }


def delete_auto_cache_entry(db_path: Path, inventory_path: Path, domain: str) -> dict[str, Any]:
    init_db(db_path, inventory_path)
    domain = normalize_domain(domain)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM domain_auto_cache WHERE domain = ?", (domain,))
    return {"ok": True, "domain": domain}


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
            SELECT r.domain, r.server_id, u.id AS user_id, u.display_name, u.client_ip
            FROM user_domain_routes r
            JOIN users u ON u.id = r.user_id
            WHERE r.enabled = 1 AND u.enabled = 1 AND u.role = 'user'
            ORDER BY u.id, r.server_id, r.domain
            """,
        )

    warnings: list[str] = []
    group_map: dict[tuple[str, str], str] = {}
    exported_routes: list[dict[str, Any]] = []
    tsv_lines = ["# group\tuser_id\tclient_ip\tinterface\tdomain\n"]

    for route in route_rows:
        user_id = route["user_id"]
        client_ip = normalize_client_ip(route["client_ip"]) if route["client_ip"] else None
        domain = route["domain"]
        server_id = route["server_id"]
        if not client_ip:
            warnings.append(f"{user_id}/{domain}: missing client_ip; skipped")
            continue
        requested_server_id = server_id
        auto_policy = None
        if requested_server_id == "auto":
            with connect(db_path) as conn:
                auto_policy = resolve_auto_candidate_policy(conn, user_id=user_id, domain=domain)
        resolved_server_id, cached = resolve_route_server(
            domain=domain,
            requested_server_id=requested_server_id,
            servers=servers,
            auto_cache=cached_auto,
            auto_policy=auto_policy,
            context=f"{user_id}/{domain}",
            warnings=warnings,
        )
        if not resolved_server_id:
            continue
        server_id = resolved_server_id
        server = servers.get(server_id)
        if not server:
            warnings.append(f"{user_id}/{domain}: unknown server {server_id}; skipped")
            continue
        if not server.get("enabled"):
            warnings.append(f"{user_id}/{domain}: server {server_id} is disabled; skipped")
            continue
        iface = safe_interface_name(server.get("interface"))
        if not iface:
            warnings.append(f"{user_id}/{domain}: server {server_id} has no safe interface; skipped")
            continue
        if server.get("kind") == "sing-box-profile":
            warnings.append(
                f"{user_id}/{domain}: {server_id} is a profile on shared interface {iface}; "
                "source routing can only select the currently active profile"
            )
        group_key = (client_ip, iface)
        group = group_map.get(group_key)
        if not group:
            group = safe_route_group(len(group_map) + 1, iface)
            group_map[group_key] = group
        tsv_lines.append(f"{group}\t{user_id}\t{client_ip}\t{iface}\t{domain}\n")
        exported_routes.append(
            {
                "group": group,
                "user_id": user_id,
                "client_ip": client_ip,
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
[ -d /etc/cudy-user-routes ] && cp -a /etc/cudy-user-routes "$backup/" || true
[ -f /usr/bin/cudy-user-routes-apply ] && cp /usr/bin/cudy-user-routes-apply "$backup/" || true
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

        commands = [
            "chmod 644 /etc/cudy-user-routes/routes.tsv /etc/cudy-user-routes/manifest.json 2>/dev/null || true",
        ]
        if install_script:
            commands.extend(
                [
                    "sh -n /usr/bin/cudy-user-routes-apply",
                    "chmod +x /usr/bin/cudy-user-routes-apply",
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


class App:
    def __init__(self, db_path: Path, inventory_path: Path):
        self.db_path = db_path
        self.inventory_path = inventory_path
        init_db(db_path, inventory_path)

    def conn(self) -> sqlite3.Connection:
        return connect(self.db_path)


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
            elif parsed.path == "/api/admin":
                self.require_admin()
                self.send_json(self.api_admin())
            elif parsed.path == "/api/route-plan":
                self.require_admin()
                self.send_json(build_route_plan(self.app.db_path))
            elif parsed.path == "/api/admin/deploy-preview":
                self.require_admin()
                self.send_json(build_combined_deploy_preview(self.app.db_path, self.app.inventory_path))
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
            elif parsed.path == "/api/admin/deploy-routes":
                self.require_admin()
                self.send_json(self.api_admin_deploy_routes(data))
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
            return {"user": user, "servers": user_servers(conn), "routes": routes}

    def api_admin(self) -> dict[str, Any]:
        with self.app.conn() as conn:
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
                "auto_cache": rows(
                    conn,
                    "SELECT domain, selected_server_id, score_ms, status, checked_at FROM domain_auto_cache ORDER BY domain",
                ),
                "auto_candidates": auto_candidate_policy_rows(conn),
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
        password = data.get("password")
        if password is not None:
            password = str(password)
            if len(password) < 8:
                raise ValueError("Password must be at least 8 characters")
        if role not in {"admin", "user"}:
            raise ValueError("role must be admin or user")
        if not user_id or not re.match(r"^[A-Za-z0-9_.-]{2,64}$", user_id):
            raise ValueError("user id must be 2-64 chars: A-Z a-z 0-9 _ . -")
        if not display_name:
            raise ValueError("display name is required")
        timestamp = now()
        with self.app.conn() as conn:
            validate_server_id(conn, default_server_id, require_user_visible=True)
            existing = row(conn, "SELECT id FROM users WHERE id = ?", (user_id,))
            if existing is None:
                if not password:
                    raise ValueError("Password is required for a new user")
                salt, password_hash = hash_password(password)
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
        return {"ok": True}

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

    def api_admin_save_global_domain_route(self, data: dict[str, Any]) -> dict[str, Any]:
        domain = normalize_domain(str(data.get("domain") or ""))
        server_id = str(data.get("server_id") or "")
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
        return {"ok": True, "domain": domain, "server_id": server_id}

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
        auto_cache_count = conn.execute("SELECT count(*) FROM domain_auto_cache").fetchone()[0]
        auto_candidate_count = conn.execute("SELECT count(*) FROM auto_candidate_policies").fetchone()[0]
    print(f"DB: {db_path}")
    print(f"Servers: {server_count} total, {user_server_count} user-visible")
    print(f"Users: {user_count} total, {login_user_count} with login")
    print(f"Domain routes: {route_count}")
    print(f"Auto cache: {auto_cache_count}")
    print(f"Auto candidate lists: {auto_candidate_count}")


def read_password_arg(value: str | None, *, confirm: bool) -> str:
    if value is not None:
        return value
    first = getpass.getpass("Password: ")
    if confirm:
        second = getpass.getpass("Confirm password: ")
        if first != second:
            raise ValueError("Passwords do not match")
    return first


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

    import_parser = sub.add_parser("import-cudy-clients", help="Import existing cudy-home client .conf files as users.")
    import_parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "secrets" / "clients" / "cudy-home",
        help="Directory with cudy-home client .conf files.",
    )

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
    if args.command == "import-cudy-clients":
        imported = import_cudy_clients(args.db, args.inventory, args.source_dir)
        for item in imported:
            print(f"{item['id']}\t{item['client_ip']}\t{item['source']}")
        print(f"Imported/updated users: {len(imported)}")
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
        print(f"Serving on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")
        finally:
            server.server_close()
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
