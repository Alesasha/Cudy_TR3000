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
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vpn_inventory.json"
DEFAULT_DB = ROOT / "data" / "vpn_control.db"
DEFAULT_USER_ID = "default"
SESSION_COOKIE = "vpn_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 210_000
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


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

CREATE TABLE IF NOT EXISTS domain_auto_cache (
  domain TEXT PRIMARY KEY,
  selected_server_id TEXT,
  score_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'unknown',
  checked_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(selected_server_id) REFERENCES servers(id)
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
        <div class="field"><label>Password</label><input id="newUserPassword" type="password" autocomplete="new-password"></div>
        <button type="submit">Create</button>
      </form>
      <p id="userStatus" class="status"></p>
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Role</th><th>Default</th><th>Enabled</th><th>Login</th><th>Password</th><th></th></tr></thead>
        <tbody id="usersBody"></tbody>
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
  </main>
  <script>
    const state = { servers: [], users: [], routes: [] };
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
    function userOptions(value) {
      return state.users.map(u => `<option value="${u.id}" ${u.id === value ? "selected" : ""}>${u.id}</option>`).join("");
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
          <td><select data-field="default_server_id">${serverOptions(u.default_server_id)}</select></td>
          <td><input type="checkbox" data-field="enabled" ${u.enabled ? "checked" : ""}></td>
          <td>${u.has_login ? "yes" : "no"}</td>
          <td class="inline"><input type="password" data-field="password" placeholder="new password"><button class="secondary" data-password="${u.id}">Set</button></td>
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
      document.getElementById("routeUser").innerHTML = userOptions(document.getElementById("routeUser").value);
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
    async function load() {
      const data = await api("/api/admin");
      state.servers = data.servers;
      state.users = data.users;
      state.routes = data.routes;
      renderServers();
      renderUsers();
      renderRoutes();
      document.getElementById("adminRouteServer").innerHTML = serverOptions(document.getElementById("adminRouteServer").value || "auto");
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
            "password_salt": "TEXT",
            "password_hash": "TEXT",
        },
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


def create_or_update_user(
    db_path: Path,
    inventory_path: Path,
    *,
    user_id: str,
    display_name: str,
    role: str,
    password: str | None,
    enabled: bool = True,
) -> None:
    if role not in {"admin", "user"}:
        raise ValueError("role must be admin or user")
    if not user_id or not re.match(r"^[A-Za-z0-9_.-]{2,64}$", user_id):
        raise ValueError("user id must be 2-64 chars: A-Z a-z 0-9 _ . -")
    init_db(db_path, inventory_path)
    timestamp = now()
    salt_hash = hash_password(password) if password is not None else (None, None)
    with connect(db_path) as conn:
        existing = row(conn, "SELECT id FROM users WHERE id = ?", (user_id,))
        if existing is None:
            if password is None:
                raise ValueError("password is required for a new user")
            conn.execute(
                """
                INSERT INTO users (
                  id, display_name, role, default_server_id, password_salt,
                  password_hash, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, 'auto', ?, ?, ?, ?, ?)
                """,
                (user_id, display_name, role, salt_hash[0], salt_hash[1], int(enabled), timestamp, timestamp),
            )
        else:
            if password is None:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, int(enabled), timestamp, user_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, password_salt = ?, password_hash = ?,
                        enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, salt_hash[0], salt_hash[1], int(enabled), timestamp, user_id),
                )


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
        if not token:
            return None
        with self.app.conn() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
            return row(
                conn,
                """
                SELECT u.id, u.display_name, u.role, u.default_server_id, u.enabled
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.expires_at > ? AND u.enabled = 1
                """,
                (token, int(time.time())),
            )

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
                "SELECT id, display_name, role, default_server_id, enabled FROM users WHERE id = ?",
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
                    SELECT id, display_name, role, default_server_id, enabled,
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
                "auto_cache": rows(
                    conn,
                    "SELECT domain, selected_server_id, score_ms, status, checked_at FROM domain_auto_cache ORDER BY domain",
                ),
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
                      id, display_name, role, default_server_id, password_salt,
                      password_hash, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, display_name, role, default_server_id, salt, password_hash, enabled, timestamp, timestamp),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, default_server_id = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, default_server_id, enabled, timestamp, user_id),
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
    print(f"DB: {db_path}")
    print(f"Servers: {server_count} total, {user_server_count} user-visible")
    print(f"Users: {user_count} total, {login_user_count} with login")
    print(f"Domain routes: {route_count}")


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
    create_user_parser.add_argument("--password", help="Prefer interactive prompt or env in normal use.")
    create_user_parser.add_argument("--disabled", action="store_true")
    create_user_parser.add_argument("--no-password-change", action="store_true")

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
            enabled=not args.disabled,
        )
        print(f"User saved: {args.user_id} role={args.role}")
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
