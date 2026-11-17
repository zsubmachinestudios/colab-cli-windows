#!/usr/bin/env python3
"""
colab-cli  -  an unofficial, Windows-friendly clone of the Google Colab CLI.

Backend : Colab MCP endpoint (https://colaboratory.googleapis.com/mcp) using the
          JSON-RPC tools list_runtimes, list_runtime_specs, create_runtime,
          execute_code, delete_session ...
UI      : rich (tables, panels, spinners) + prompt_toolkit (multi-line REPL).

All 23 commands:
  Session   : new, sessions, status, restart-kernel, stop, url
  Execution : run, exec, repl, ssh
  Files     : ls, upload, download, rm, edit
  Utilities : auth, drivemount, install, log, usage, pay, version, update
"""
from __future__ import annotations

import argparse
import base64
import builtins
import codeop
import datetime as dt
import hashlib
import json
import keyword
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import uuid
import warnings
import webbrowser
from pathlib import Path

__version__ = "1.1.0"

# --------------------------------------------------------------------------- #
#  Dependencies
# --------------------------------------------------------------------------- #
try:
    import requests
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (BarColumn, DownloadColumn, Progress, SpinnerColumn,
                               TextColumn, TimeElapsedColumn, TransferSpeedColumn)
    from rich.prompt import Confirm
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
except ImportError as _e:  # pragma: no cover
    print(f"Missing dependency: {_e.name}\n"
          "Install with:  pip install rich requests google-auth google-auth-oauthlib prompt_toolkit")
    sys.exit(1)

# UTF-8 everywhere (Windows consoles default to cp1252)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)
log = logging.getLogger("colab-cli")

MCP_URL = "https://colaboratory.googleapis.com/mcp"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform",
          "https://www.googleapis.com/auth/colaboratory"]
LOGIN_SCOPES = SCOPES + ["openid", "https://www.googleapis.com/auth/userinfo.email"]
COLAB_HOME = "https://colab.research.google.com/"
COLAB_PAY = "https://colab.research.google.com/signup"
MARK = "__COLABCLI__"
REMOTE_CWD = "/content"

ORANGE, AMBER, MUTED = "#F9AB00", "#E8710A", "grey62"


# --------------------------------------------------------------------------- #
#  Errors & helpers
# --------------------------------------------------------------------------- #
class ColabError(Exception):
    def __init__(self, msg: str, hint: str | None = None):
        super().__init__(msg)
        self.hint = hint


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def short_id(resource: str) -> str:
    return resource.rstrip("/").split("/")[-1] if resource else "-"


def ok(msg: str):
    console.print(f"[bold green]✔[/] {msg}")


def info(msg: str):
    console.print(f"[{ORANGE}]●[/] {msg}")


def warn(msg: str):
    err_console.print(f"[bold yellow]![/] {msg}")


def atomic_write(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def clean_err(e) -> str:
    """Turn HTML error pages (502 etc.) into one readable line."""
    t = str(e)
    if "<html" in t.lower() or "<!doctype" in t.lower():
        m = re.search(r"<b>(\d{3})\.?</b>", t) or re.search(r"Error (\d{3})", t)
        code = m.group(1) if m else "5xx"
        return f"Google server error {code} (temporary)"
    return t if len(t) < 600 else t[:600] + " …"


TRANSIENT_AUTH = ("502", "503", "504", "500", "429", "temporar", "timed out", "timeout",
                  "connection", "reset", "unavailable", "<html", "<!doctype")


def refresh_with_retry(creds, attempts=6):
    """Refresh OAuth/ADC credentials, retrying transient Google errors with backoff."""
    from google.auth.transport.requests import Request
    last = None
    for i in range(attempts):
        try:
            creds.refresh(Request())
            return creds
        except Exception as e:
            last = e
            txt = str(e).lower()
            if "invalid_grant" in txt or "invalid_scope" in txt or "unauthorized_client" in txt:
                raise
            if not any(k in txt for k in TRANSIENT_AUTH) or i == attempts - 1:
                raise
            wait = min(2 ** i * 2, 30)
            log.debug("token refresh transient error (%s), retry in %ss", clean_err(e), wait)
            err_console.print(f"[{MUTED}]  Google auth server busy ({clean_err(e)}) - retrying in {wait}s…[/]")
            time.sleep(wait)
    raise last


def hint_for(err_text: str) -> str | None:
    t = err_text.upper()
    if "INSUFFICIENT AUTHENTICATION SCOPES" in t or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in t:
        return ("Your login is missing the Colab scope. Run:  colab auth login   "
                "(requests https://www.googleapis.com/auth/colaboratory)")
    if "SERVICE_DISABLED" in t or "HAS NOT BEEN USED" in t:
        return "Enable the Colab API for your project in Google Cloud Console, then retry."
    if "UNAUTHENTICATED" in t or "INVALID_GRANT" in t or "401" in t:
        return "Run:  colab auth login"
    if "USER_PROJECT_DENIED" in t or "SERVICEUSAGE" in t:
        return "Your account can't bill to this project. Set your own:  colab auth --set-project YOUR_PROJECT_ID"
    if "PERMISSION_DENIED" in t or "403" in t:
        return "Check the project (colab auth --set-project ID) and that your account has access."
    if "RESOURCE_EXHAUSTED" in t:
        return "Quota / rate limit reached. Wait a bit, or check  colab usage."
    if "NOT_FOUND" in t:
        return "The runtime or session no longer exists. Run  colab sessions  to resync."
    return None


# --------------------------------------------------------------------------- #
#  Config & local state
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "colab-cli"


class Config:
    DEFAULTS = {
        "project_id": None,
        "auth": "adc",                 # adc | oauth2
        "client_oauth_config": None,   # path to OAuth client json (oauth2 mode)
        "update_source": None,         # URL or path used by `colab update`
        "exec_timeout": 660,
    }

    def __init__(self, path: Path | None = None):
        self.dir = config_dir()
        self.path = Path(path) if path else self.dir / "config.json"
        self.data = dict(self.DEFAULTS)
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception:
                warn(f"Config file is corrupt, using defaults: {self.path}")

    def get(self, key, default=None):
        v = self.data.get(key)
        return default if v is None else v

    def set(self, key, value):
        self.data[key] = value
        atomic_write(self.path, json.dumps(self.data, indent=2))

    @property
    def sessions_path(self) -> Path:
        return self.dir / "sessions.json"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def token_path(self) -> Path:
        return self.dir / "oauth_token.json"


class SessionStore:
    """Local metadata: friendly name -> runtime + kernel session."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.sessions_path
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                warn("sessions.json was corrupt - starting fresh.")
                self.data = {}

    def save(self):
        atomic_write(self.path, json.dumps(self.data, indent=2))

    def add(self, name, runtime, spec=None, adopted=False):
        self.data[name] = {
            "runtime": runtime,
            "kernel": new_kernel_session(runtime),
            "spec": spec or {},
            "created": now_iso(),
            "adopted": adopted,
            "cwd": REMOTE_CWD,
        }
        self.save()
        return self.data[name]

    def remove(self, name):
        self.data.pop(name, None)
        self.save()

    def by_runtime(self, runtime):
        for n, s in self.data.items():
            if s.get("runtime") == runtime:
                return n
        return None

    def unique_name(self, base):
        base = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-") or "session"
        if base not in self.data:
            return base
        i = 2
        while f"{base}-{i}" in self.data:
            i += 1
        return f"{base}-{i}"


def new_kernel_session(runtime: str) -> str:
    # session_id: lowercase letters/digits/hyphens, starts with a letter
    return f"{runtime}/sessions/py-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
#  Auth
# --------------------------------------------------------------------------- #
class Auth:
    def __init__(self, cfg: Config, mode: str, client_cfg: str | None):
        self.cfg, self.mode, self.client_cfg = cfg, mode, client_cfg
        self._creds = None
        self.adc_project = None

    def creds(self):
        if self._creds is None:
            self._creds = self._load()
        if not self._creds.valid:
            try:
                refresh_with_retry(self._creds)
            except Exception as e:
                raise ColabError(f"Could not refresh credentials: {clean_err(e)}", self._hint(e)) from e
            if self.mode == "oauth2":
                self._save_oauth()
        return self._creds

    def token(self) -> str:
        return self.creds().token

    @staticmethod
    def _hint(e) -> str:
        t = str(e).lower()
        if any(k in t for k in TRANSIENT_AUTH):
            return "Google's auth server is having a temporary problem. Wait ~30 s and run the same command again."
        return "Run:  colab auth login"

    def _load(self):
        import google.auth
        import google.auth.exceptions
        if self.mode == "oauth2":
            return self._load_oauth()
        try:
            creds, proj = google.auth.default(scopes=SCOPES)
            self.adc_project = getattr(creds, "quota_project_id", None) or proj
        except google.auth.exceptions.DefaultCredentialsError as e:
            raise ColabError("No Application Default Credentials found.",
                             "Run:  colab auth login   (needs gcloud)  or use --auth oauth2 -c client.json") from e
        try:
            return refresh_with_retry(creds)
        except Exception as e:
            raise ColabError(f"ADC authentication failed: {clean_err(e)}", self._hint(e)) from e

    def _load_oauth(self):
        from google.oauth2.credentials import Credentials
        tp = self.cfg.token_path
        if tp.exists():
            try:
                return Credentials.from_authorized_user_file(str(tp), SCOPES)
            except Exception:
                warn("Cached OAuth token unreadable, re-authenticating.")
        return self.oauth_login()

    def oauth_login(self):
        client = self.client_cfg or self.cfg.get("client_oauth_config")
        if not client or not Path(client).exists():
            raise ColabError("OAuth2 mode needs a client config JSON.",
                             "Use:  colab --auth oauth2 -c path\\to\\client_secret.json auth login")
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as e:
            raise ColabError("google-auth-oauthlib is not installed.",
                             "pip install google-auth-oauthlib") from e
        flow = InstalledAppFlow.from_client_secrets_file(client, LOGIN_SCOPES)
        self._creds = flow.run_local_server(port=0, open_browser=True)
        self._save_oauth()
        return self._creds

    def _save_oauth(self):
        if self._creds is not None and hasattr(self._creds, "to_json"):
            atomic_write(self.cfg.token_path, self._creds.to_json())

    def account(self) -> str:
        c = self.creds()
        for attr in ("service_account_email", "account", "_account"):
            v = getattr(c, attr, None)
            if v:
                return v
        try:
            r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                             params={"access_token": c.token}, timeout=15)
            return r.json().get("email", "unknown")
        except Exception:
            return "unknown"


# --------------------------------------------------------------------------- #
#  MCP backend
# --------------------------------------------------------------------------- #
class Backend:
    RETRYABLE = ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED")

    def __init__(self, cfg: Config, auth: Auth, project: str | None):
        self.cfg, self.auth = cfg, auth
        self._project = project
        self._tools: dict | None = None
        self.http = requests.Session()

    @property
    def project(self) -> str:
        if not self._project:
            self.auth.creds()
            self._project = (self.cfg.get("project_id") or os.environ.get("COLAB_PROJECT")
                             or os.environ.get("GOOGLE_CLOUD_PROJECT") or self.auth.adc_project)
            if not self._project:
                raise ColabError("No Google Cloud project configured.",
                                 "Run:  colab auth --set-project YOUR_PROJECT_ID")
        return self._project

    def headers(self):
        return {
            "Authorization": f"Bearer {self.auth.token()}",
            "x-goog-user-project": self.project,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    @staticmethod
    def _parse(resp, label):
        raw = resp.text.strip()
        if not raw:
            raise ColabError(f"Empty response (HTTP {resp.status_code}) from {label}",
                             hint_for(str(resp.status_code)))
        data = None
        if raw.startswith("data:") or "\ndata:" in raw:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
        else:
            try:
                data = json.loads(raw)
            except Exception as e:
                raise ColabError(f"Non-JSON response (HTTP {resp.status_code}) from {label}: {clean_err(raw)}",
                                 hint_for(raw)) from e
        if not data:
            raise ColabError(f"No JSON payload (HTTP {resp.status_code}) from {label}: {clean_err(raw)}")
        return data

    def _post(self, payload, label, timeout, idempotent, retries=4):
        last = None
        refreshed = False
        for attempt in range(retries):
            try:
                log.debug("POST %s %s", label, json.dumps(payload)[:500])
                resp = self.http.post(MCP_URL, headers=self.headers(), json=payload, timeout=timeout)
                log.debug("HTTP %s %s", resp.status_code, resp.text[:500])
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                if idempotent and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                if isinstance(e, requests.Timeout):
                    raise ColabError(f"{label} timed out after {timeout}s.",
                                     "The remote job may still be running. Use a longer --timeout, "
                                     "or run it in the background with  colab ssh -- 'nohup ... &'") from e
                raise ColabError(f"Network error calling {label}: {e}", "Check your internet / proxy.") from e
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self.auth._creds = None
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            data = self._parse(resp, label)
            if "error" in data:
                err = json.dumps(data["error"], indent=2)
                if any(k in err for k in self.RETRYABLE) and attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise ColabError(f"{label} failed (HTTP {resp.status_code}):\n{err}", hint_for(err))
            return data
        raise ColabError(f"{label} failed after {retries} attempts: {last}")

    # ---- tool discovery ------------------------------------------------- #
    def tools(self) -> dict:
        if self._tools is None:
            data = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              "tools/list", 60, True)
            self._tools = {t["name"]: t for t in data.get("result", {}).get("tools", [])}
        return self._tools

    def find_tool(self, *candidates, contains: tuple = ()) -> str | None:
        try:
            names = self.tools()
        except ColabError:
            return candidates[0] if candidates else None
        for c in candidates:
            if c in names:
                return c
        for n in names:
            if contains and all(k in n.lower() for k in contains):
                return n
        return None

    def arg_name(self, tool, fallback):
        try:
            schema = self.tools().get(tool, {}).get("inputSchema", {})
            req = schema.get("required") or list(schema.get("properties", {}))
            return req[0] if req else fallback
        except Exception:
            return fallback

    def call(self, tool, arguments, timeout=120, idempotent=True, retries=4):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}}
        for attempt in range(retries):
            data = self._post(payload, tool, timeout, idempotent)
            result = data.get("result", {})
            if result.get("isError"):
                err = json.dumps(result, indent=2)
                if any(k in err for k in self.RETRYABLE) and attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise ColabError(f"Colab tool '{tool}' returned an error:\n{err[:1500]}", hint_for(err))
            if "structuredContent" in result:
                return result["structuredContent"]
            for block in result.get("content", []):
                if block.get("type") == "text":
                    try:
                        return json.loads(block["text"])
                    except Exception:
                        return {"text": block["text"]}
            return result
        raise ColabError(f"{tool} failed after {retries} retries")

    # ---- runtime API ---------------------------------------------------- #
    def list_runtimes(self) -> list[dict]:
        return self.call("list_runtimes", {}).get("runtimes", []) or []

    def list_specs(self) -> list[dict]:
        return self.call("list_runtime_specs", {}).get("runtimeSpecs", []) or []

    def get_runtime(self, name) -> dict | None:
        tool = self.find_tool("get_runtime")
        if tool:
            try:
                return self.call(tool, {self.arg_name(tool, "name"): name})
            except ColabError:
                pass
        return next((r for r in self.list_runtimes() if r.get("name") == name), None)

    def create_runtime(self, spec_key) -> str:
        before = {r.get("name") for r in self.list_runtimes()}
        created = self.call("create_runtime", {"requestId": str(uuid.uuid4()),
                                               "runtime": {"runtimeSpec": spec_key}},
                            timeout=600, idempotent=False)
        name = created.get("name", "")
        if "/runtimes/" not in name:
            name = (created.get("response") or created.get("runtime") or {}).get("name", "")
        if "/runtimes/" not in name:   # long-running op: find the new runtime ourselves
            for _ in range(60):
                new = [r.get("name") for r in self.list_runtimes() if r.get("name") not in before]
                if new:
                    return new[0]
                time.sleep(5)
            raise ColabError("Runtime creation did not return a runtime name.",
                             f"Raw response: {json.dumps(created)[:500]}")
        return name

    def delete_runtime(self, name):
        tool = self.find_tool("delete_runtime", "stop_runtime", "terminate_runtime",
                              "unassign_runtime", contains=("runtime", "delete"))
        if not tool:
            raise ColabError("This backend exposes no runtime-delete tool.",
                             "Run  colab tools  to see available backend tools.")
        return self.call(tool, {self.arg_name(tool, "name"): name}, timeout=300)

    def delete_session(self, session):
        tool = self.find_tool("delete_session") or "delete_session"
        return self.call(tool, {self.arg_name(tool, "name"): session}, timeout=60)

    def execute(self, session, code, timeout=None) -> "ExecResult":
        out = self.call("execute_code", {"session": session, "code": code},
                        timeout=timeout or int(self.cfg.get("exec_timeout", 660)), idempotent=False)
        return ExecResult(out)


# --------------------------------------------------------------------------- #
#  Execution results
# --------------------------------------------------------------------------- #
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ExecResult:
    def __init__(self, raw: dict):
        self.raw = raw or {}
        self.stdout = self.raw.get("stdout", "") or self.raw.get("text", "") or ""
        self.stderr = self.raw.get("stderr", "") or ""
        self.result = self.raw.get("result")
        self.error = self.raw.get("executionError")
        self.payloads, clean = [], []
        for line in self.stdout.splitlines(keepends=True):
            if line.startswith(MARK):
                try:
                    self.payloads.append(json.loads(line[len(MARK):]))
                except json.JSONDecodeError:
                    pass
            else:
                clean.append(line)
        self.stdout = "".join(clean)

    @property
    def ok(self):
        return not self.error

    @property
    def payload(self) -> dict:
        return self.payloads[-1] if self.payloads else {}

    def error_text(self) -> str:
        e = self.error
        if not e:
            return ""
        if isinstance(e, dict):
            tb = e.get("traceback")
            if tb:
                return ANSI_RE.sub("", "\n".join(tb) if isinstance(tb, list) else str(tb))
            return f"{e.get('ename', 'Error')}: {e.get('evalue', json.dumps(e))}"
        return str(e)

    def to_dict(self):
        return {"stdout": self.stdout, "stderr": self.stderr, "result": self.result,
                "error": self.error_text() or None}

    def render(self, show_result=True, out_label="Out: "):
        if self.stdout:
            sys.stdout.write(self.stdout if self.stdout.endswith("\n") else self.stdout + "\n")
            sys.stdout.flush()
        if self.stderr:
            err_console.print(Text(self.stderr.rstrip("\n"), style="yellow"))
        if show_result and self.result not in (None, "", {}):
            r = self.result if isinstance(self.result, str) else json.dumps(self.result, indent=2)
            console.print(Text(out_label, style=f"bold {AMBER}") + Text(str(r)))
        if self.error:
            err_console.print(Panel(Text(self.error_text()), title="[bold red]Execution error",
                                    border_style="red", box=box.ROUNDED))
        self._save_images()

    def _save_images(self):
        found = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("image/png", "image/jpeg") and isinstance(v, str):
                        found.append((k, v))
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(self.raw)
        if found:
            out = Path("colab_outputs")
            out.mkdir(exist_ok=True)
            for mime, b64 in found:
                p = out / f"output-{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.{mime.split('/')[1]}"
                p.write_bytes(base64.b64decode(b64))
                info(f"Saved image output → {p}")


# --------------------------------------------------------------------------- #
#  App: glue between CLI, store and backend
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, args):
        self.args = args
        self.cfg = Config(getattr(args, "config", None))
        mode = getattr(args, "auth", None) or self.cfg.get("auth", "adc")
        client = getattr(args, "client_oauth_config", None)
        self.auth = Auth(self.cfg, mode, client)
        self.backend = Backend(self.cfg, self.auth, getattr(args, "project", None))
        self.store = SessionStore(self.cfg)
        self._synced = False

    # ---- session sync / resolution ------------------------------------- #
    def sync(self, quiet=False) -> list[dict]:
        runtimes = self.backend.list_runtimes()
        live = {r.get("name") for r in runtimes}
        changed = False
        for name, s in list(self.store.data.items()):
            if s.get("runtime") not in live:
                if not quiet:
                    warn(f"Session [bold]{name}[/] no longer exists on the backend - removed.")
                self.store.data.pop(name)
                changed = True
        for r in runtimes:
            if not self.store.by_runtime(r.get("name")):
                acc = spec_accel(r) or "cpu"
                nm = self.store.unique_name(f"{acc_label(acc)}-{short_id(r.get('name'))[:6]}")
                self.store.data[nm] = {"runtime": r.get("name"),
                                       "kernel": new_kernel_session(r.get("name")),
                                       "spec": r.get("runtimeSpec", {}), "created": now_iso(),
                                       "adopted": True, "cwd": REMOTE_CWD}
                changed = True
        if changed:
            self.store.save()
        self._synced = True
        return runtimes

    def resolve(self, name: str | None) -> tuple[str, dict]:
        if name and name in self.store.data:
            return name, self.store.data[name]
        if not self._synced:
            self.sync(quiet=True)
        if name:
            if name in self.store.data:
                return name, self.store.data[name]
            matches = [n for n, s in self.store.data.items()
                       if name in n or name in s.get("runtime", "")]
            if len(matches) == 1:
                return matches[0], self.store.data[matches[0]]
            raise ColabError(f"No session named '{name}'.", "List them with:  colab sessions")
        names = list(self.store.data)
        if not names:
            raise ColabError("No active session.", "Create one with:  colab new   (or  colab new --gpu T4)")
        if len(names) == 1:
            return names[0], self.store.data[names[0]]
        if sys.stdin.isatty():
            console.print("[bold]Multiple sessions active:[/]")
            for i, n in enumerate(names, 1):
                console.print(f"  [{ORANGE}]{i}[/]  {n}")
            choice = console.input("Pick a session number: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                n = names[int(choice) - 1]
                return n, self.store.data[n]
        raise ColabError("Multiple sessions active - specify one with -s NAME.",
                         "Sessions: " + ", ".join(names))

    # ---- execution ------------------------------------------------------ #
    def execute(self, name, sess, code, label="exec", spinner="Running on Colab…",
                timeout=None, record=True) -> ExecResult:
        t0 = time.time()
        try:
            with console.status(f"[{ORANGE}]{spinner}", spinner="dots"):
                res = self.backend.execute(sess["kernel"], code, timeout)
        except KeyboardInterrupt:
            warn("Interrupted locally. The remote cell may still be running - "
                 "use  colab restart-kernel  if it hangs.")
            raise
        except ColabError as e:
            if "NOT_FOUND" in str(e).upper():
                sess["kernel"] = new_kernel_session(sess["runtime"])
                self.store.save()
                with console.status(f"[{ORANGE}]Kernel session expired - reconnecting…"):
                    res = self.backend.execute(sess["kernel"], code, timeout)
            else:
                raise
        res.seconds = time.time() - t0
        if record:
            self.record(name, label, code, res, res.seconds)
        return res

    def helper(self, name, sess, code, spinner, timeout=None) -> dict:
        """Run internal helper code; return its JSON payload or raise."""
        res = self.execute(name, sess, code, spinner=spinner, timeout=timeout, record=False)
        if res.error:
            raise ColabError(f"Remote helper failed:\n{res.error_text()}")
        p = res.payload
        if p.get("error"):
            raise ColabError(p["error"])
        return p

    def record(self, name, kind, code, res: ExecResult, secs):
        try:
            self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)
            entry = {"ts": now_iso(), "kind": kind, "seconds": round(secs, 2), "code": code,
                     **res.to_dict()}
            with open(self.cfg.logs_dir / f"{name}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            log.debug("log write failed: %s", e)


# --------------------------------------------------------------------------- #
#  Spec helpers
# --------------------------------------------------------------------------- #
def _find_key(obj, words) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(w in k.lower() for w in words) and isinstance(v, (str, int)):
                return str(v)
        for v in obj.values():
            r = _find_key(v, words)
            if r:
                return r
    return None


def spec_accel(spec) -> str | None:
    a = _find_key(spec, ("accel",))
    if a and a.upper() in ("NONE", "ACCELERATOR_NONE", "ACCELERATOR_UNSPECIFIED"):
        return None
    return a


def spec_shape(spec) -> str:
    return _find_key(spec, ("shape", "machine", "memory")) or "STANDARD"


def acc_label(acc: str | None) -> str:
    if not acc:
        return "cpu"
    a = acc.upper()
    for tok in ("H100", "A100", "L4", "T4", "G4", "V6E", "V5E", "V2", "V3"):
        if tok in a.replace("_", ""):
            return ("tpu-" if tok.startswith("V") else "gpu-") + tok.lower()
    return re.sub(r"[^a-z0-9]+", "-", a.lower()).strip("-")[:16]


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def pick_spec(specs, gpu=None, tpu=None, high_mem=False):
    eligible = [s for s in specs if s.get("eligible", True)]
    if not eligible:
        raise ColabError("No eligible runtime specs for this account.", "Check  colab usage  /  colab pay")

    def want_acc(s):
        acc = spec_accel(s.get("key", s))
        if not gpu and not tpu:
            return acc is None
        if not acc:
            return False
        tok = norm(gpu or tpu)
        na = norm(acc)
        if tpu and "TPU" not in na and not na.startswith("V"):
            return False
        return tok in na or tok.replace("1", "") in na if tpu else tok in na

    cands = [s for s in eligible if want_acc(s)]
    if not cands:
        what = f"GPU {gpu}" if gpu else f"TPU {tpu}" if tpu else "CPU"
        avail = ", ".join(sorted({acc_label(spec_accel(s.get("key", s))) for s in eligible}))
        raise ColabError(f"No eligible runtime for {what}.", f"Eligible on your account: {avail}  "
                         "(see  colab new --list)")
    hm = [s for s in cands if "HIGH" in norm(spec_shape(s.get("key", s)))]
    std = [s for s in cands if s not in hm]
    if high_mem:
        if not hm:
            warn("No high-RAM shape available for that accelerator - using standard.")
        return (hm or std)[0]
    return (std or hm)[0]


# --------------------------------------------------------------------------- #
#  Remote helper code snippets (run inside the Colab kernel)
# --------------------------------------------------------------------------- #
def py_wrap(body: str) -> str:
    """Wrap helper code so it always emits exactly one JSON payload line."""
    return (
        "def __colabcli():\n"
        "    import json as _j, os, sys, base64, hashlib, shutil, subprocess\n"
        f"    _M = {MARK!r}\n"
        "    def emit(o): print(_M + _j.dumps(o, default=str))\n"
        "    try:\n"
        + textwrap.indent(textwrap.dedent(body), "        ")
        + "\n    except Exception as _e:\n"
        "        emit({'error': f'{type(_e).__name__}: {_e}'})\n"
        "__colabcli(); del __colabcli\n"
    )


def remote_path(p: str, cwd: str = REMOTE_CWD) -> str:
    if not p:
        return cwd
    if p.startswith("~"):
        p = "/root" + p[1:]
    if not p.startswith("/"):
        p = cwd.rstrip("/") + "/" + p
    return p


PROBE_CODE = py_wrap("""
import platform, time
d = {'python': sys.version.split()[0], 'platform': platform.platform(),
     'cpu_count': os.cpu_count(), 'hostname': platform.node()}
try:
    mem = dict(l.split(':', 1) for l in open('/proc/meminfo'))
    d['ram_total'] = int(mem['MemTotal'].split()[0]) * 1024
    d['ram_avail'] = int(mem['MemAvailable'].split()[0]) * 1024
except Exception: pass
try:
    du = shutil.disk_usage('/content' if os.path.exists('/content') else '/')
    d['disk_total'], d['disk_free'] = du.total, du.free
except Exception: pass
try:
    d['uptime_s'] = float(open('/proc/uptime').read().split()[0])
except Exception: pass
try:
    q = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu',
                        '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=20)
    if q.returncode == 0 and q.stdout.strip():
        d['gpus'] = [x.strip() for x in q.stdout.strip().splitlines()]
except Exception: pass
tpu = [x for x in os.listdir('/dev') if x.startswith('accel') or x.startswith('vfio')] if os.path.exists('/dev') else []
if tpu or os.environ.get('TPU_NAME') or os.environ.get('COLAB_TPU_ADDR'):
    d['tpu'] = os.environ.get('TPU_ACCELERATOR_TYPE') or os.environ.get('TPU_NAME') or f'{len(tpu)} device(s)'
d['drive_mounted'] = os.path.ismount('/content/drive')
emit(d)
""")


def shell_code(cmd: str, cwd: str, timeout: int | None = None) -> str:
    script = f"cd {shlex.quote(cwd)} 2>/dev/null || cd /content 2>/dev/null || cd ~\n{cmd}\n" \
             "__rc=$?\npwd > /tmp/.colabcli_cwd\nexit $__rc\n"
    return py_wrap(f"""
p = subprocess.run(['bash', '-c', {script!r}], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   text=True, errors='replace', timeout={timeout!r})
sys.stdout.write(p.stdout)
if p.stdout and not p.stdout.endswith('\\n'): print()
try: cwd = open('/tmp/.colabcli_cwd').read().strip()
except Exception: cwd = {cwd!r}
emit({{'rc': p.returncode, 'cwd': cwd}})
""")


def remote_shell(app: App, name, sess, cmd, spinner="Running…", cwd=None, timeout=None, record=True):
    cwd = cwd or sess.get("cwd", REMOTE_CWD)
    res = app.execute(name, sess, shell_code(cmd, cwd), label="shell", spinner=spinner,
                      timeout=timeout, record=False)
    if record:
        app.record(name, "shell", f"!{cmd}", res, getattr(res, "seconds", 0))
    p = res.payload
    if res.error and not p:
        raise ColabError(res.error_text())
    if p.get("error"):
        raise ColabError(p["error"])
    return res, p.get("rc", 1), p.get("cwd", cwd)


# --------------------------------------------------------------------------- #
#  File transfer
# --------------------------------------------------------------------------- #
UP_CHUNK = 2 * 1024 * 1024
DOWN_CHUNK = 1024 * 1024


def _progress(quiet):
    return Progress(SpinnerColumn(style=ORANGE), TextColumn("[bold]{task.description}"),
                    BarColumn(complete_style=ORANGE), DownloadColumn(), TransferSpeedColumn(),
                    TimeElapsedColumn(), console=console, disable=quiet)


def upload_file(app: App, name, sess, local: Path, remote: str, quiet=False):
    if not local.is_file():
        raise ColabError(f"Local file not found: {local}")
    size = local.stat().st_size
    md5 = hashlib.md5()
    with _progress(quiet) as prog, open(local, "rb") as fh:
        task = prog.add_task(f"↑ {local.name}", total=max(size, 1))
        first = True
        while True:
            chunk = fh.read(UP_CHUNK)
            if not chunk and not first:
                break
            md5.update(chunk)
            b64 = base64.b64encode(chunk).decode()
            code = py_wrap(f"""
p = {remote!r}
os.makedirs(os.path.dirname(p) or '/', exist_ok=True)
with open(p, {'"wb"' if first else '"ab"'}) as f: f.write(base64.b64decode({b64!r}))
emit({{'ok': True}})
""")
            res = app.backend.execute(sess["kernel"], code)
            if res.error or res.payload.get("error") or not res.payload.get("ok"):
                raise ColabError(f"Upload failed for {local}: {res.payload.get('error') or res.error_text()}")
            prog.update(task, advance=len(chunk) or 1)
            first = False
            if not chunk:
                break
    chk = py_wrap(f"""
h = hashlib.md5()
with open({remote!r}, 'rb') as f:
    for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
emit({{'md5': h.hexdigest(), 'size': os.path.getsize({remote!r})}})
""")
    p = app.backend.execute(sess["kernel"], chk).payload
    if p.get("md5") != md5.hexdigest():
        raise ColabError(f"Checksum mismatch after uploading {local.name} - please retry.")
    if not quiet:
        ok(f"Uploaded [bold]{local.name}[/] → {remote}  ({human_size(size)}, md5 verified)")


def download_file(app: App, name, sess, remote: str, local: Path, quiet=False):
    meta = app.helper(name, sess, py_wrap(f"""
p = {remote!r}
if not os.path.isfile(p): raise FileNotFoundError(p)
h = hashlib.md5()
with open(p, 'rb') as f:
    for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
emit({{'size': os.path.getsize(p), 'md5': h.hexdigest()}})
"""), "Checking remote file…")
    size, md5 = meta["size"], hashlib.md5()
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_name(local.name + ".part")
    with _progress(quiet) as prog, open(tmp, "wb") as out:
        task = prog.add_task(f"↓ {Path(remote).name}", total=max(size, 1))
        off = 0
        while off < size or (off == 0 and size == 0):
            res = app.backend.execute(sess["kernel"], py_wrap(f"""
with open({remote!r}, 'rb') as f:
    f.seek({off}); b = f.read({DOWN_CHUNK})
emit({{'d': base64.b64encode(b).decode()}})
"""))
            if res.error or "d" not in res.payload:
                raise ColabError(f"Download failed: {res.payload.get('error') or res.error_text()}")
            data = base64.b64decode(res.payload["d"])
            out.write(data)
            md5.update(data)
            off += len(data)
            prog.update(task, completed=off)
            if not data:
                break
    if md5.hexdigest() != meta["md5"]:
        tmp.unlink(missing_ok=True)
        raise ColabError("Checksum mismatch during download - please retry.")
    os.replace(tmp, local)
    if not quiet:
        ok(f"Downloaded {remote} → [bold]{local}[/]  ({human_size(size)}, md5 verified)")


def list_remote_tree(app, name, sess, remote) -> list[str]:
    p = app.helper(name, sess, py_wrap(f"""
root = {remote!r}
out = []
for d, _, fs in os.walk(root):
    for f in fs: out.append(os.path.join(d, f))
emit({{'files': out}})
"""), "Scanning remote folder…")
    return p.get("files", [])


# --------------------------------------------------------------------------- #
#  Notebook helpers
# --------------------------------------------------------------------------- #
def notebook_cells(path: Path) -> list[str]:
    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ColabError(f"Could not read notebook {path}: {e}")
    cells = []
    for c in nb.get("cells", []):
        if c.get("cell_type") == "code":
            src = c.get("source", "")
            src = "".join(src) if isinstance(src, list) else src
            if src.strip():
                cells.append(src)
    return cells


def build_notebook(entries: list[dict]) -> dict:
    cells = []
    for i, e in enumerate(entries, 1):
        outputs = []
        if e.get("stdout"):
            outputs.append({"output_type": "stream", "name": "stdout", "text": e["stdout"]})
        if e.get("stderr"):
            outputs.append({"output_type": "stream", "name": "stderr", "text": e["stderr"]})
        if e.get("result") not in (None, ""):
            outputs.append({"output_type": "execute_result", "execution_count": i, "metadata": {},
                            "data": {"text/plain": str(e["result"])}})
        if e.get("error"):
            outputs.append({"output_type": "error", "ename": "Error", "evalue": "",
                            "traceback": str(e["error"]).splitlines()})
        cells.append({"cell_type": "code", "execution_count": i, "metadata": {},
                      "source": e.get("code", ""), "outputs": outputs})
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"},
                         "colab": {"provenance": []}}}


def run_cells(app, name, sess, cells, label="exec") -> list[dict]:
    results = []
    for i, src in enumerate(cells, 1):
        console.rule(f"[{ORANGE}]Cell {i}/{len(cells)}", style=MUTED)
        res = app.execute(name, sess, src, label=label, spinner=f"Running cell {i}/{len(cells)}…")
        res.render()
        results.append({"code": src, **res.to_dict()})
        if res.error:
            warn(f"Stopped at cell {i} because of an error.")
            break
    return results


# --------------------------------------------------------------------------- #
#  UI: banner & help
# --------------------------------------------------------------------------- #
LOGO = [
    " ██████  ██████  ██       █████  ██████ ",
    "██      ██    ██ ██      ██   ██ ██   ██",
    "██      ██    ██ ██      ███████ ██████ ",
    "██      ██    ██ ██      ██   ██ ██   ██",
    " ██████  ██████  ███████ ██   ██ ██████ ",
]

COMMANDS = {
    "Session management": [
        ("new", "[-s NAME] [--gpu GPU] [--tpu TPU] [--high-mem]", "Allocate a CPU / GPU / TPU runtime"),
        ("sessions", "", "List active sessions on the backend"),
        ("status", "[-s NAME]", "Hardware, shape, state & local metadata"),
        ("restart-kernel", "[-s NAME]", "Restart the Jupyter kernel"),
        ("stop", "[-s NAME | --all]", "Terminate the VM and release hardware"),
        ("url", "[-s NAME] [--open]", "Print / open a browser URL for the session"),
    ],
    "Code execution": [
        ("run", "[--gpu ..] [--keep] TARGET [ARGS]", "Fresh VM → run script/command → tear down"),
        ("exec", "[-s NAME] [FILE | -c CODE | stdin]", "Run Python from stdin, .py or .ipynb"),
        ("repl", "[-s NAME]", "Interactive multi-line Python REPL on the VM"),
        ("ssh", "[-s NAME] [-- COMMAND]", "Remote shell or one-shot command"),
    ],
    "File operations": [
        ("ls", "[-s NAME] [PATH] [-l] [-a]", "List remote files"),
        ("upload", "[-s NAME] LOCAL [-d REMOTE]", "Upload files / folders"),
        ("download", "[-s NAME] REMOTE [LOCAL]", "Download files / folders"),
        ("rm", "[-s NAME] PATH [-r] [-f]", "Delete remote files"),
        ("edit", "[-s NAME] PATH", "Edit a remote file in your local $EDITOR"),
    ],
    "Automation & utilities": [
        ("auth", "[login|status|logout|remote]", "Local login / GCP auth inside the VM"),
        ("drivemount", "[-s NAME]", "Mount Google Drive at /content/drive"),
        ("install", "[-s NAME] PKG...", "Install packages via uv / pip"),
        ("log", "[-s NAME] [--export FILE]", "View / export logs (.ipynb .md .txt .jsonl)"),
        ("usage", "", "Compute-unit balance & usage"),
        ("pay", "", "Manage your Colab subscription"),
        ("version", "", "Show CLI version"),
        ("update", "[--source URL|PATH]", "Upgrade the CLI in place"),
    ],
}


def banner():
    grad = ["#FFC107", "#FFB300", ORANGE, "#F08A00", AMBER]
    t = Text()
    for line, c in zip(LOGO, grad):
        t.append(line + "\n", style=f"bold {c}")
    t.append(f"  Google Colab from your terminal  ·  v{__version__}  ·  {platform.system()}",
             style=MUTED)
    console.print(Panel(t, box=box.ROUNDED, border_style=ORANGE, padding=(0, 2), expand=False))


def print_help():
    banner()
    console.print(f"\n[bold]Usage:[/] colab [GLOBAL FLAGS] [{ORANGE}]COMMAND[/] [ARGS]\n")
    for cat, cmds in COMMANDS.items():
        tb = Table(box=None, show_header=False, padding=(0, 2), pad_edge=False)
        tb.add_column(style=f"bold {ORANGE}", no_wrap=True, min_width=15)
        tb.add_column(style=MUTED)
        tb.add_column()
        for c, a, d in cmds:
            tb.add_row(c, Text(a), d)
        console.print(Panel(tb, title=f"[bold]{cat}", title_align="left", border_style=MUTED,
                            box=box.ROUNDED))
    g = Table(box=None, show_header=False, padding=(0, 2))
    g.add_column(style=f"bold {ORANGE}", no_wrap=True)
    g.add_column()
    for f, d in [("-h, --help", "Show help (also: colab COMMAND --help)"),
                 ("--auth {adc,oauth2}", "Authentication strategy"),
                 ("-c, --client-oauth-config FILE", "OAuth client JSON (oauth2 mode)"),
                 ("--config FILE", "Custom config / metadata file path"),
                 ("--project ID", "GCP project used for quota/billing"),
                 ("--logtostderr", "Debug logging to stderr")]:
        g.add_row(Text(f), d)
    console.print(Panel(g, title="[bold]Global flags", title_align="left", border_style=MUTED,
                        box=box.ROUNDED))
    console.print(f"[{MUTED}]Tip: with a single active session, -s/--session can be omitted.[/]")


# --------------------------------------------------------------------------- #
#  Commands — Session management
# --------------------------------------------------------------------------- #
def cmd_new(app: App, a):
    if a.gpu and a.tpu:
        raise ColabError("Choose either --gpu or --tpu, not both.")
    with console.status(f"[{ORANGE}]Fetching runtime specs…"):
        specs = app.backend.list_specs()
    if a.list:
        tb = Table(title="Runtime specs", box=box.ROUNDED, border_style=MUTED, header_style=f"bold {ORANGE}")
        tb.add_column("Accelerator")
        tb.add_column("Shape")
        tb.add_column("Eligible", justify="center")
        tb.add_column("Key", style=MUTED)
        for s in specs:
            k = s.get("key", s)
            tb.add_row(acc_label(spec_accel(k)), spec_shape(k),
                       "[green]✔[/]" if s.get("eligible", True) else "[red]✘[/]", Text(json.dumps(k)))
        console.print(tb)
        return
    spec = pick_spec(specs, a.gpu, a.tpu, a.high_mem)
    key = spec.get("key", spec)
    label = acc_label(spec_accel(key))
    if a.session and a.session in app.store.data:
        raise ColabError(f"Session '{a.session}' already exists.", "Pick another -s NAME.")
    name = a.session or app.store.unique_name(label)
    info(f"Allocating [bold]{label}[/] ({spec_shape(key)}) as session [bold]{name}[/]")
    with console.status(f"[{ORANGE}]Requesting VM from Colab… (can take a minute)"):
        runtime = app.backend.create_runtime(key)
    sess = app.store.add(name, runtime, key)
    wait_ready(app, name, sess)
    console.print(Panel(
        f"[bold]Session[/]  {name}\n[bold]Runtime[/]  {runtime}\n[bold]Hardware[/] {label} · {spec_shape(key)}",
        title="[bold green]Runtime ready", border_style="green", box=box.ROUNDED, expand=False))
    console.print(f"[{MUTED}]Next:  colab exec -c \"print('hi')\"   ·   colab repl   ·   colab ssh[/]")


def wait_ready(app, name, sess, max_wait=600):
    t0 = time.time()
    with console.status(f"[{ORANGE}]Booting kernel…") as st:
        while True:
            try:
                r = app.backend.execute(sess["kernel"], "print('__ready__')", timeout=120)
                if "__ready__" in r.stdout:
                    return
            except ColabError as e:
                if time.time() - t0 > max_wait:
                    raise ColabError(f"Runtime did not become ready in {max_wait}s: {e}")
                log.debug("not ready yet: %s", e)
            st.update(f"[{ORANGE}]Booting kernel… {int(time.time() - t0)}s")
            time.sleep(5)


def cmd_sessions(app: App, a):
    with console.status(f"[{ORANGE}]Syncing with backend…"):
        runtimes = {r.get("name"): r for r in app.sync()}
    if not app.store.data:
        console.print(Panel("No active sessions.\nStart one with  [bold]colab new[/]  or  "
                            "[bold]colab new --gpu T4[/]", border_style=MUTED, box=box.ROUNDED, expand=False))
        return
    tb = Table(box=box.ROUNDED, border_style=MUTED, header_style=f"bold {ORANGE}", title="Active sessions")
    for c in ("Name", "Hardware", "Shape", "State", "Created", "Runtime ID"):
        tb.add_column(c)
    for n, s in app.store.data.items():
        r = runtimes.get(s["runtime"], {})
        spec = s.get("spec") or r.get("runtimeSpec") or r
        state = str(r.get("state") or r.get("status") or "RUNNING")
        color = "green" if state.upper() in ("RUNNING", "ACTIVE", "READY") else "yellow"
        tb.add_row(f"[bold]{n}[/]" + (" [dim](adopted)[/]" if s.get("adopted") else ""),
                   acc_label(spec_accel(spec)), spec_shape(spec), f"[{color}]● {state}[/]",
                   s.get("created", "-").replace("T", " "), short_id(s["runtime"]))
    console.print(tb)


def cmd_status(app: App, a):
    names = [a.session] if a.session else None
    if not names:
        app.sync(quiet=True)
        names = list(app.store.data)
        if not names:
            raise ColabError("No active session.", "Create one with:  colab new")
    for n in names:
        name, sess = app.resolve(n)
        rt = app.backend.get_runtime(sess["runtime"]) or {}
        probe = {}
        if not a.no_probe:
            try:
                probe = app.helper(name, sess, PROBE_CODE, f"Probing {name}…")
            except ColabError as e:
                warn(f"Hardware probe failed: {e}")
        spec = sess.get("spec") or rt.get("runtimeSpec") or rt
        tb = Table(box=None, show_header=False, padding=(0, 2))
        tb.add_column(style=f"bold {ORANGE}", no_wrap=True)
        tb.add_column()
        rows = [
            ("Runtime", sess["runtime"]),
            ("State", str(rt.get("state") or rt.get("status") or "RUNNING")),
            ("Accelerator", acc_label(spec_accel(spec))),
            ("Machine shape", spec_shape(spec)),
            ("Kernel session", short_id(sess["kernel"])),
            ("Created (local)", sess.get("created", "-")),
            ("Remote cwd", sess.get("cwd", REMOTE_CWD)),
        ]
        if probe:
            rows += [
                ("Python", probe.get("python", "-")),
                ("OS", probe.get("platform", "-")),
                ("CPU cores", str(probe.get("cpu_count", "-"))),
            ]
            if "ram_total" in probe:
                used = probe["ram_total"] - probe["ram_avail"]
                rows.append(("RAM", f"{human_size(used)} used / {human_size(probe['ram_total'])}"))
            if "disk_total" in probe:
                rows.append(("Disk", f"{human_size(probe['disk_free'])} free / {human_size(probe['disk_total'])}"))
            for i, g in enumerate(probe.get("gpus", [])):
                gname, mt, mu, ut = (g.split(",") + ["", "", "", ""])[:4]
                rows.append((f"GPU {i}", f"{gname.strip()} · {mu.strip()}/{mt.strip()} MiB · {ut.strip()}% util"))
            if probe.get("tpu"):
                rows.append(("TPU", str(probe["tpu"])))
            if "uptime_s" in probe:
                rows.append(("Uptime", str(dt.timedelta(seconds=int(probe["uptime_s"])))))
            rows.append(("Drive mounted", "yes" if probe.get("drive_mounted") else "no"))
        for k, v in rows:
            tb.add_row(k, Text(str(v)))
        console.print(Panel(tb, title=f"[bold]{name}", border_style=ORANGE, box=box.ROUNDED, expand=False))


def restart_kernel(app: App, name, sess):
    with console.status(f"[{ORANGE}]Restarting kernel…"):
        try:
            app.backend.delete_session(sess["kernel"])
        except ColabError as e:
            log.debug("delete_session: %s", e)
        sess["kernel"] = new_kernel_session(sess["runtime"])
        sess["cwd"] = REMOTE_CWD
        app.store.save()
        app.backend.execute(sess["kernel"], "pass", timeout=120)
    ok(f"Kernel restarted for [bold]{name}[/] (all variables cleared).")


def cmd_restart_kernel(app: App, a):
    name, sess = app.resolve(a.session)
    restart_kernel(app, name, sess)


def cmd_stop(app: App, a):
    if a.all:
        app.sync(quiet=True)
        targets = list(app.store.data)
        if not targets:
            info("Nothing to stop.")
            return
    else:
        targets = [app.resolve(a.session)[0]]
    if not a.yes and sys.stdin.isatty():
        if not Confirm.ask(f"Stop {', '.join(targets)}? Unsaved files on the VM will be lost"):
            info("Cancelled.")
            return
    for n in targets:
        stop_session(app, n)


def stop_session(app: App, name):
    sess = app.store.data.get(name)
    if not sess:
        return
    with console.status(f"[{ORANGE}]Stopping {name}…"):
        try:
            app.backend.delete_session(sess["kernel"])
        except ColabError:
            pass
        try:
            app.backend.delete_runtime(sess["runtime"])
        except ColabError as e:
            if "NOT_FOUND" not in str(e).upper():
                raise
    app.store.remove(name)
    ok(f"Stopped [bold]{name}[/] and released its hardware.")


def cmd_url(app: App, a):
    name, sess = app.resolve(a.session)
    rt = app.backend.get_runtime(sess["runtime"]) or {}
    url = None
    for k, v in rt.items():
        if isinstance(v, str) and v.startswith("http"):
            url = v
            break
    url = url or COLAB_HOME
    console.print(Panel(f"[bold]{url}[/]\n[{MUTED}]Runtime: {sess['runtime']}[/]",
                        title=f"[bold]{name}", border_style=ORANGE, box=box.ROUNDED, expand=False))
    if url == COLAB_HOME:
        console.print(f"[{MUTED}]The backend did not expose a direct runtime URL; this opens Colab.[/]")
    if a.open:
        webbrowser.open(url)
        ok("Opened in your browser.")


# --------------------------------------------------------------------------- #
#  Commands — Code execution
# --------------------------------------------------------------------------- #
def cmd_run(app: App, a):
    if a.target and a.target[0] == "--":
        a.target = a.target[1:]
    if not a.target:
        raise ColabError("Nothing to run.", "Example:  colab run train.py --epochs 3   or   colab run -- nvidia-smi")
    specs = app.backend.list_specs()
    spec = pick_spec(specs, a.gpu, a.tpu, a.high_mem)
    key = spec.get("key", spec)
    name = app.store.unique_name(a.session or f"run-{acc_label(spec_accel(key))}")
    info(f"Spinning up a fresh [bold]{acc_label(spec_accel(key))}[/] VM ([bold]{name}[/])")
    with console.status(f"[{ORANGE}]Requesting VM…"):
        runtime = app.backend.create_runtime(key)
    sess = app.store.add(name, runtime, key)
    rc = 0
    try:
        wait_ready(app, name, sess)
        for extra in a.with_files or []:
            p = Path(extra)
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        upload_file(app, name, sess, f, f"/content/{f.relative_to(p.parent).as_posix()}", quiet=True)
                ok(f"Uploaded folder {p}")
            else:
                upload_file(app, name, sess, p, f"/content/{p.name}")
        target, rest = a.target[0], a.target[1:]
        tp = Path(target)
        if tp.is_file() and tp.suffix == ".ipynb":
            res = run_cells(app, name, sess, notebook_cells(tp), label="run")
            rc = 1 if res and res[-1].get("error") else 0
        else:
            if tp.is_file():
                upload_file(app, name, sess, tp, f"/content/{tp.name}")
                interp = "bash" if tp.suffix == ".sh" else "python"
                cmd = " ".join([interp, shlex.quote(tp.name)] + [shlex.quote(x) for x in rest])
            else:
                cmd = " ".join(a.target) if len(a.target) > 1 else target
            console.rule(f"[{ORANGE}]$ {cmd}", style=MUTED)
            res, rc, _ = remote_shell(app, name, sess, cmd, spinner=f"Running {cmd[:40]}…", timeout=None)
            res.render(show_result=False)
        for d in a.download or []:
            fetch(app, name, sess, d, Path(Path(d).name))
        console.rule(style=MUTED)
        (ok if rc == 0 else warn)(f"Finished with exit code {rc}")
    finally:
        if a.keep:
            info(f"VM kept alive as session [bold]{name}[/]  (stop later:  colab stop -s {name})")
        else:
            try:
                stop_session(app, name)
            except Exception as e:
                warn(f"Teardown failed - run  colab stop -s {name}  manually ({e})")
    if rc:
        sys.exit(rc)


def cmd_exec(app: App, a):
    name, sess = app.resolve(a.session)
    if a.code:
        src, kind = a.code, "code"
    elif a.file and a.file != "-":
        p = Path(a.file)
        if not p.is_file():
            raise ColabError(f"File not found: {p}")
        if p.suffix == ".ipynb":
            results = run_cells(app, name, sess, notebook_cells(p))
            if a.save:
                atomic_write(Path(a.save), json.dumps(build_notebook(results), indent=1))
                ok(f"Saved executed notebook → {a.save}")
            if results and results[-1].get("error"):
                sys.exit(1)
            return
        src, kind = p.read_text(encoding="utf-8"), p.name
    elif not sys.stdin.isatty() or a.file == "-":
        src, kind = sys.stdin.read(), "stdin"
    else:
        raise ColabError("No code given.", 'Use:  colab exec script.py | colab exec -c "print(1)" | type x.py | colab exec')
    if not src.strip():
        raise ColabError("The code is empty.")
    res = app.execute(name, sess, src, label="exec", spinner=f"Executing {kind} on {name}…",
                      timeout=a.timeout)
    res.render()
    if res.error:
        sys.exit(1)


# ======================= Multi-line REPL engine ============================ #
INDENT = "    "
_DEDENT_AFTER = ("return", "pass", "break", "continue", "raise")
_DEDENT_KW = {"else": ("if", "elif", "for", "while", "try", "except"),
              "elif": ("if", "elif"),
              "except": ("try", "except"),
              "finally": ("try", "except", "else"),
              "case": ("match", "case")}
_LOCAL_MAGICS = ("%help", "%run", "%load", "%edit", "%hist", "%history", "%reset", "%clear",
                 "%cls", "%exit", "%quit")


def _strip_comment(line: str) -> str:
    """Remove a trailing # comment, ignoring #'s inside simple quotes."""
    out, q = [], None
    for ch in line:
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _bracket_depth(text: str) -> int:
    depth, q = 0, None
    for line in text.split("\n"):
        for ch in _strip_comment(line):
            if q:
                if ch == q:
                    q = None
            elif ch in "'\"":
                q = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
        q = None
    return depth


def repl_should_submit(text: str) -> bool:
    """IPython-style decision: run the buffer now, or keep editing?"""
    if not text.strip():
        return True
    lines = text.split("\n")
    first = lines[0].lstrip()
    if len(lines) == 1 and first.startswith(("!", "%", ":")) and not first.startswith("%%"):
        return True
    if first.startswith("%%"):                       # cell magic: blank line ends it
        return len(lines) > 1 and not lines[-1].strip()
    if len(lines) > 1 and not lines[-1].strip():
        return True                                  # blank line closes a block
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            code = codeop.compile_command(text, "<input>", "exec")
    except (SyntaxError, ValueError, OverflowError):
        # still inside an indented block?  keep editing instead of erroring early
        return len(lines) == 1 or not lines[-1][:1].isspace()
    if code is None:
        return False                                 # incomplete (def/if/open bracket…)
    if len(lines) == 1:
        return True
    opens_block = any(_strip_comment(l).rstrip().endswith(":") or l.lstrip().startswith("@")
                      for l in lines)
    if not opens_block:
        return True                                  # e.g. x = [1,\n 2] -> complete, run it
    return not lines[-1][:1].isspace()


def repl_next_indent(text_before_cursor: str) -> str:
    """Indent for the next line, given everything typed so far."""
    line = text_before_cursor.split("\n")[-1]
    cur = len(line) - len(line.lstrip(" \t"))
    code = _strip_comment(line).rstrip()
    stripped = code.strip()
    if _bracket_depth(text_before_cursor) > 0 and _bracket_depth(line) > 0:
        return " " * (cur + 4)
    if stripped.endswith(":"):
        return " " * (cur + 4)
    first = re.split(r"[\s(]", stripped, maxsplit=1)[0] if stripped else ""
    if first in _DEDENT_AFTER:
        return " " * max(0, cur - 4)
    return " " * cur


def repl_auto_dedent(text: str) -> str | None:
    """If the current (last) line starts with else/elif/except/finally, return its corrected
    indentation based on the matching opener above, else None."""
    lines = text.split("\n")
    line = lines[-1]
    s = line.lstrip()
    kw = re.match(r"(else|elif|except|finally|case)\b", s)
    if not kw:
        return None
    cur = len(line) - len(s)
    for prev in reversed(lines[:-1]):
        ps = prev.strip()
        if not ps or ps.startswith("#"):
            continue
        pind = len(prev) - len(prev.lstrip())
        opener = re.match(r"(%s)\b" % "|".join(_DEDENT_KW[kw.group(1)]), ps)
        if opener and pind == cur:
            return None                       # already aligned with its opener
        if opener and pind < cur:
            return " " * pind
    return None


class _ReplUI:
    """prompt_toolkit powered multi-line editor (falls back to plain input)."""

    def __init__(self, app: App, name: str):
        self.app, self.name = app, name
        self.n = 1
        self.pt = _ensure_prompt_toolkit()
        self.words = set(keyword.kwlist) | {w for w in dir(builtins) if not w.startswith("_")}
        self.next_default = ""
        if self.pt:
            self._build_session()

    # ---- prompt_toolkit session ---------------------------------------- #
    def _build_session(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.lexers import PygmentsLexer
        from prompt_toolkit.styles import Style, merge_styles, style_from_pygments_cls
        from pygments.lexers.python import PythonLexer
        from pygments.styles import get_style_by_name

        ui = self

        class WordCompleter(Completer):
            def get_completions(self, document, complete_event):
                w = document.get_word_before_cursor(WORD=False)
                if not w or not (w[0].isalpha() or w[0] == "_"):
                    return
                for x in sorted(ui.words):
                    if x.startswith(w) and x != w:
                        yield Completion(x, start_position=-len(w))

        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event):
            b = event.current_buffer
            if b.complete_state and b.complete_state.current_completion:
                b.apply_completion(b.complete_state.current_completion)
                return
            b.cancel_completion()
            doc = b.document
            if not doc.is_cursor_at_the_end:                 # editing in the middle
                b.insert_text("\n" + repl_next_indent(doc.text_before_cursor))
                return
            if repl_should_submit(doc.text):
                b.validate_and_handle()
                return
            b.insert_text("\n" + repl_next_indent(doc.text))

        @kb.add("escape", "enter")
        @kb.add("c-s")
        def _force_run(event):
            event.current_buffer.validate_and_handle()

        @kb.add("tab")
        def _tab(event):
            b = event.current_buffer
            before = b.document.current_line_before_cursor
            if not before.strip():
                b.insert_text(" " * (4 - len(before) % 4))
            else:
                b.start_completion(insert_common_part=True)

        @kb.add("s-tab")
        def _shift_tab(event):
            b = event.current_buffer
            line = b.document.current_line
            lead = len(line) - len(line.lstrip(" "))
            n = (lead % 4 or 4) if lead else 0
            if n:
                pos = b.cursor_position
                b.cursor_position = pos - b.document.cursor_position_col
                b.delete(n)
                b.cursor_position = max(b.cursor_position, pos - n)

        @kb.add("backspace")
        def _backspace(event):
            b = event.current_buffer
            before = b.document.current_line_before_cursor
            if before and not before.strip():
                n = len(before) % 4 or 4
                b.delete_before_cursor(min(n, len(before)))
            else:
                b.delete_before_cursor(1)

        @kb.add(":")
        def _colon(event):
            b = event.current_buffer
            b.insert_text(":")
            if b.document.is_cursor_at_the_end:
                fix = repl_auto_dedent(b.document.text)
                if fix is not None:
                    line = b.document.current_line
                    cur = len(line) - len(line.lstrip())
                    if len(fix) < cur:
                        pos = b.cursor_position
                        b.cursor_position = pos - b.document.cursor_position_col
                        b.delete(cur - len(fix))
                        b.cursor_position = pos - (cur - len(fix))

        @kb.add("c-c")
        def _cancel(event):
            b = event.current_buffer
            if b.text:
                b.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt)

        style = merge_styles([
            style_from_pygments_cls(get_style_by_name("monokai")),
            Style.from_dict({
                "prompt": f"bold {ORANGE}",
                "prompt.num": "bold #ffffff",
                "cont": "#6c6c6c",
                "bottom-toolbar": "noreverse bg:#262626 #a8a8a8",
                "bottom-toolbar.key": "noreverse bg:#262626 bold #F9AB00",
                "auto-suggestion": "#585858",
            }),
        ])
        hist_path = self.app.cfg.dir / "repl_history"
        try:
            hist = FileHistory(str(hist_path))
        except Exception:
            hist = InMemoryHistory()

        def toolbar():
            keys = [("Enter", "run / newline"), ("blank line", "end block"), ("Esc+Enter", "force run"),
                    ("Tab", "indent/complete"), ("Ctrl+C", "clear"), ("Ctrl+D", "quit"), ("%help", "")]
            parts = [("class:bottom-toolbar", f" ● {ui.name}  ")]
            for k, d in keys:
                parts += [("class:bottom-toolbar.key", k), ("class:bottom-toolbar", f" {d}  " if d else "  ")]
            return FormattedText(parts)

        self.FormattedText = FormattedText
        self.session = PromptSession(
            lexer=PygmentsLexer(PythonLexer), style=style, key_bindings=kb, history=hist,
            auto_suggest=AutoSuggestFromHistory(), completer=WordCompleter(),
            complete_while_typing=False, multiline=True, bottom_toolbar=toolbar,
            prompt_continuation=self._continuation, include_default_pygments_style=False,
            enable_history_search=False, mouse_support=False, **_PT_IO)

    def _prompt_width(self):
        return len(f"In [{self.n}]: ")

    def _continuation(self, width, line_number, is_soft_wrap):
        w = self._prompt_width()
        dots = "...: "
        return self.FormattedText([("class:cont", " " * max(0, w - len(dots)) + dots)])

    # ---- reading ------------------------------------------------------- #
    def read(self) -> str:
        if self.pt:
            msg = self.FormattedText([("class:prompt", "In ["), ("class:prompt.num", str(self.n)),
                                      ("class:prompt", "]: ")])
            default, self.next_default = self.next_default, ""
            return self.session.prompt(msg, default=default)
        return self._read_plain()

    def _read_plain(self) -> str:
        buf = []
        w = self._prompt_width()
        while True:
            p = f"[bold {ORANGE}]In [{self.n}]:[/] " if not buf else f"[{MUTED}]{' ' * (w - 5)}...:[/] "
            buf.append(console.input(p))
            if repl_should_submit("\n".join(buf)):
                return "\n".join(buf)

    def learn(self, src: str):
        self.words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", src))


_PT_IO: dict = {}   # test hook: inject prompt_toolkit input/output


def _ensure_prompt_toolkit() -> bool:
    try:
        import prompt_toolkit  # noqa: F401
        import pygments  # noqa: F401
        return True
    except ImportError:
        pass
    if not sys.stdin.isatty():
        return False
    info("Installing [bold]prompt_toolkit[/] for the multi-line editor (one-time)…")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "prompt_toolkit>=3.0", "pygments"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            import importlib
            importlib.invalidate_caches()
            import prompt_toolkit  # noqa: F401,F811
            import pygments  # noqa: F401,F811
            ok("Installed.")
            return True
        log.debug("pip failed: %s", r.stderr)
    except Exception as e:
        log.debug("pip install prompt_toolkit failed: %s", e)
    warn("Could not install prompt_toolkit - using the basic REPL (pip install prompt_toolkit).")
    return False


def _repl_help():
    tb = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ORANGE}", show_edge=False)
    tb.add_column("Key / command", style="bold", no_wrap=True)
    tb.add_column("What it does")
    for k, d in [
        ("Enter", "Run if the code is complete, otherwise new line with auto-indent"),
        ("Enter on blank line", "Finish & run a multi-line block (like python)"),
        ("Esc then Enter / Ctrl+S", "Force run now, whatever the state"),
        ("Tab / Shift+Tab", "Indent 4 spaces (or complete a name) / dedent"),
        ("Backspace in indent", "Remove one indent level"),
        ("↑ / ↓", "Move between lines; at first/last line browse history"),
        ("Paste", "Multi-line paste keeps its own indentation"),
        ("Ctrl+C", "Clear the current input (twice on empty input = quit)"),
        ("Ctrl+D  /  exit()", "Quit the REPL (session keeps running)"),
        ("!cmd", "Run a shell command on the VM (e.g. !nvidia-smi, !pip install x)"),
        ("%run file.py [args]", "Run a LOCAL .py/.ipynb on the VM"),
        ("%load file.py", "Load a LOCAL file into the editor to tweak before running"),
        ("%edit", "Edit the last cell in your $EDITOR, then run it"),
        ("%hist", "Show cells run in this REPL"),
        ("%reset", "Restart the kernel (clears variables)"),
        ("%clear", "Clear the screen"),
        ("other %magics", "Sent to the Colab kernel as usual (%timeit, %%time, …)"),
    ]:
        tb.add_row(k, d)
    console.print(Panel(tb, title="[bold]REPL help", border_style=ORANGE, box=box.ROUNDED, expand=False))


def cmd_repl(app: App, a):
    name, sess = app.resolve(a.session)
    ui = _ReplUI(app, name)
    mode = "multi-line editor · syntax colours · auto-indent" if ui.pt else "basic mode"
    console.print(Panel(
        f"Connected to [bold]{name}[/] · {mode}\n"
        f"[{MUTED}]Blank line runs a block · Esc+Enter forces run · !cmd = shell · %help = all keys · "
        "Ctrl+D quits[/]",
        title="[bold]Colab Python REPL", border_style=ORANGE, box=box.ROUNDED, expand=False))
    history: list[str] = []
    last_ctrl_c = 0.0
    while True:
        try:
            src = ui.read()
        except KeyboardInterrupt:
            if time.time() - last_ctrl_c < 1.5:
                break
            last_ctrl_c = time.time()
            console.print(f"[{MUTED}]KeyboardInterrupt — press Ctrl+C again or Ctrl+D to quit[/]")
            continue
        except EOFError:
            break
        src = "\n".join(l.rstrip() for l in src.split("\n")).strip("\n")
        if not src.strip():
            continue
        s = src.strip()
        word = s.split()[0] if s.split() else ""
        if s in ("exit", "exit()", "quit", "quit()", ":q", "%exit", "%quit"):
            break
        try:
            # ---- local magics ----
            if word in ("%help", "?"):
                _repl_help()
                continue
            if word in ("%clear", "%cls"):
                console.clear()
                continue
            if word in ("%hist", "%history"):
                for i, h in enumerate(history, 1):
                    console.print(f"[bold {ORANGE}]In [{i}]:[/]")
                    console.print(Syntax(h, "python", theme="monokai", background_color="default"))
                if not history:
                    console.print(f"[{MUTED}](no history yet)[/]")
                continue
            if s in (":reset", "%reset", "%reset -f"):
                restart_kernel(app, name, sess)
                sess = app.store.data[name]
                ui.n = 1
                continue
            if word == "%load" and len(s.split()) > 1:
                p = Path(s.split(maxsplit=1)[1].strip().strip('"'))
                if p.is_file():
                    if ui.pt:
                        ui.next_default = p.read_text(encoding="utf-8").rstrip("\n")
                        info(f"Loaded {p.name} into the editor - edit it, then Esc+Enter to run.")
                    else:
                        warn("%load needs prompt_toolkit; use %run instead.")
                    continue
            if word == "%run" and len(s.split()) > 1:
                parts = shlex.split(s[4:], posix=os.name != "nt")
                p = Path(parts[0].strip('"'))
                if p.is_file():
                    if p.suffix == ".ipynb":
                        run_cells(app, name, sess, notebook_cells(p), label="repl")
                    else:
                        code = (f"import sys as __s; __s.argv = {[p.name] + parts[1:]!r}\n"
                                + p.read_text(encoding="utf-8"))
                        res = app.execute(name, sess, code, label="repl", spinner=f"Running {p.name}…")
                        res.render(out_label=f"Out[{ui.n}]: ")
                        _repl_time(res)
                    history.append(s)
                    ui.n += 1
                    continue
            if word == "%edit":
                base = history[-1] if history else ""
                tmp = Path(tempfile.mkdtemp(prefix="colab-repl-")) / "cell.py"
                tmp.write_text(base, encoding="utf-8")
                _open_editor(tmp)
                new = tmp.read_text(encoding="utf-8").strip("\n")
                shutil.rmtree(tmp.parent, ignore_errors=True)
                if not new.strip():
                    continue
                console.print(Syntax(new, "python", theme="monokai", background_color="default"))
                src = s = new
            # ---- shell ----
            if s.startswith("!") and "\n" not in s:
                res, rc, sess["cwd"] = remote_shell(app, name, sess, s[1:], spinner=f"$ {s[1:40]}")
                app.store.save()
                res.render(show_result=False)
                if rc:
                    console.print(f"[{MUTED}]exit {rc}[/]")
                history.append(s)
                ui.n += 1
                continue
            # ---- python ----
            res = app.execute(name, sess, src, label="repl", spinner="Running…")
            res.render(out_label=f"Out[{ui.n}]: ")
            _repl_time(res)
            ui.learn(src)
            history.append(src)
            ui.n += 1
        except KeyboardInterrupt:
            console.print(f"[{MUTED}]Interrupted (the remote cell may still be running).[/]")
        except ColabError as e:
            show_error(e)
    ok("REPL closed - the session keeps running.")


def _repl_time(res):
    secs = getattr(res, "seconds", 0) or 0
    if secs >= 2:
        console.print(f"[{MUTED}]⏱ {secs:.1f}s[/]")


def _open_editor(path: Path):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or \
        ("notepad" if os.name == "nt" else (shutil.which("nano") and "nano") or "vi")
    try:
        cmd = shlex.split(editor, posix=os.name != "nt") + [str(path)]
        if Path(cmd[0]).stem.lower() == "code" and "--wait" not in cmd:
            cmd.insert(1, "--wait")
        subprocess.call(cmd, shell=(os.name == "nt" and not shutil.which(cmd[0])))
    except FileNotFoundError:
        raise ColabError(f"Editor not found: {editor}", "Set the EDITOR variable, e.g.  set EDITOR=notepad")
    return editor


# ============================== ssh ======================================= #
def cmd_ssh(app: App, a):
    if a.proxy:
        raise ColabError("OpenSSH ProxyCommand bridging is not available through the Colab MCP API.",
                         "Use  colab ssh  (interactive) or  colab ssh -- COMMAND  instead.")
    name, sess = app.resolve(a.session)
    if a.command and a.command[0] == "--":
        a.command = a.command[1:]
    cmd = " ".join(a.command).strip()
    if cmd:  # one-shot
        res, rc, sess["cwd"] = remote_shell(app, name, sess, cmd, spinner=f"$ {cmd[:50]}", timeout=a.timeout)
        app.store.save()
        res.render(show_result=False)
        sys.exit(rc)
    console.print(Panel(
        f"Shell on [bold]{name}[/] · cwd changes persist via cd\n"
        f"[{MUTED}]Each command runs to completion (no full-screen apps like vim/top). "
        "Type exit to leave.[/]",
        title="[bold]Colab shell", border_style=ORANGE, box=box.ROUNDED, expand=False))
    reader = None
    if _ensure_prompt_toolkit():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style
        ps = PromptSession(history=FileHistory(str(app.cfg.dir / "ssh_history")),
                           style=Style.from_dict({"u": "bold ansigreen", "p": "bold ansiblue"}), **_PT_IO)
        reader = lambda cwd: ps.prompt(FormattedText([("class:u", "root@colab"), ("", ":"),  # noqa: E731
                                                      ("class:p", cwd), ("", "$ ")]))
    while True:
        cwd = sess.get("cwd", REMOTE_CWD)
        try:
            line = (reader(cwd) if reader else
                    console.input(f"[bold green]root@colab[/]:[bold blue]{cwd}[/]$ ")).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print()
            break
        if not line:
            continue
        if line in ("exit", "logout", "quit"):
            break
        try:
            res, rc, sess["cwd"] = remote_shell(app, name, sess, line, spinner=line[:50], timeout=a.timeout)
            app.store.save()
            res.render(show_result=False)
            if rc:
                console.print(f"[{MUTED}]exit {rc}[/]")
        except KeyboardInterrupt:
            warn("Interrupted locally (remote command may continue).")
        except ColabError as e:
            show_error(e)
    ok("Shell closed - the session keeps running.")


# --------------------------------------------------------------------------- #
#  Commands — File operations
# --------------------------------------------------------------------------- #
def cmd_ls(app: App, a):
    name, sess = app.resolve(a.session)
    path = remote_path(a.path, sess.get("cwd", REMOTE_CWD))
    p = app.helper(name, sess, py_wrap(f"""
p = {path!r}
if not os.path.exists(p): raise FileNotFoundError(p)
if os.path.isfile(p):
    st = os.stat(p); emit({{'entries': [[os.path.basename(p), False, st.st_size, st.st_mtime]]}})
else:
    out = []
    for e in os.scandir(p):
        try: st = e.stat(follow_symlinks=False); out.append([e.name, e.is_dir(), st.st_size, st.st_mtime])
        except Exception: out.append([e.name, e.is_dir(), 0, 0])
    emit({{'entries': out}})
"""), f"Listing {path}…")
    entries = sorted(p.get("entries", []), key=lambda e: (not e[1], e[0].lower()))
    if not a.all:
        entries = [e for e in entries if not e[0].startswith(".")]
    if not entries:
        console.print(f"[{MUTED}](empty) {path}[/]")
        return
    if a.long:
        tb = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ORANGE}", title=path, title_justify="left")
        tb.add_column("Name")
        tb.add_column("Size", justify="right")
        tb.add_column("Modified", style=MUTED)
        for nm, isdir, size, mt in entries:
            tb.add_row(Text(nm + "/", style="bold blue") if isdir else Text(nm),
                       "-" if isdir else human_size(size),
                       dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M") if mt else "-")
        console.print(tb)
    else:
        from rich.columns import Columns
        console.print(Columns([Text(nm + "/", style="bold blue") if d else Text(nm) for nm, d, *_ in entries],
                              padding=(0, 3)))


def cmd_upload(app: App, a):
    name, sess = app.resolve(a.session)
    cwd = sess.get("cwd", REMOTE_CWD)
    srcs = [Path(s) for s in a.local]
    dest = a.remote
    for src in srcs:
        if not src.exists():
            raise ColabError(f"Local path not found: {src}")
        if src.is_dir():
            base = remote_path(dest or src.name, cwd)
            files = [f for f in src.rglob("*") if f.is_file()]
            info(f"Uploading folder {src} ({len(files)} files) → {base}")
            for f in files:
                upload_file(app, name, sess, f, f"{base}/{f.relative_to(src).as_posix()}", quiet=True)
                console.print(f"  [green]✔[/] {f.relative_to(src)}")
            ok(f"Folder uploaded → {base}")
        else:
            if dest and (dest.endswith("/") or len(srcs) > 1):
                target = remote_path(dest.rstrip("/") + "/" + src.name, cwd)
            else:
                target = remote_path(dest or src.name, cwd)
            upload_file(app, name, sess, src, target)


def fetch(app, name, sess, remote_arg, local: Path | None):
    rp = remote_path(remote_arg, sess.get("cwd", REMOTE_CWD))
    kind = app.helper(name, sess, py_wrap(f"""
p = {rp!r}
emit({{'dir': os.path.isdir(p), 'exists': os.path.exists(p)}})
"""), "Checking…")
    if not kind.get("exists"):
        raise ColabError(f"Remote path not found: {rp}")
    if kind.get("dir"):
        files = list_remote_tree(app, name, sess, rp)
        base = local or Path(Path(rp).name)
        info(f"Downloading folder {rp} ({len(files)} files) → {base}")
        for f in files:
            rel = f[len(rp.rstrip("/")):].lstrip("/")
            download_file(app, name, sess, f, base / rel, quiet=True)
            console.print(f"  [green]✔[/] {rel}")
        ok(f"Folder downloaded → {base}")
    else:
        target = local or Path(Path(rp).name)
        if target.is_dir():
            target = target / Path(rp).name
        download_file(app, name, sess, rp, target)


def cmd_download(app: App, a):
    name, sess = app.resolve(a.session)
    local = None
    if a.local:
        local = Path(a.local)
        if a.local.endswith(("/", "\\")):
            local.mkdir(parents=True, exist_ok=True)
    fetch(app, name, sess, a.remote, local)


def cmd_rm(app: App, a):
    name, sess = app.resolve(a.session)
    paths = [remote_path(p, sess.get("cwd", REMOTE_CWD)) for p in a.paths]
    for p in paths:
        if os.path.normpath(p).replace("\\", "/") in ("/", "/content", "/root", "/usr", "/bin", "/etc"):
            raise ColabError(f"Refusing to delete protected path {p}.")
    if not a.force and sys.stdin.isatty():
        if not Confirm.ask(f"Delete {', '.join(paths)}?"):
            info("Cancelled.")
            return
    p = app.helper(name, sess, py_wrap(f"""
done, errs = [], []
for p in {paths!r}:
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            if not {bool(a.recursive)!r}: raise IsADirectoryError(f'{{p}} is a directory (use -r)')
            shutil.rmtree(p)
        else:
            os.remove(p)
        done.append(p)
    except Exception as e: errs.append(f'{{type(e).__name__}}: {{e}}')
emit({{'done': done, 'errs': errs}})
"""), "Deleting…")
    for d in p.get("done", []):
        ok(f"Deleted {d}")
    for e in p.get("errs", []):
        warn(e)
    if p.get("errs"):
        sys.exit(1)


def cmd_edit(app: App, a):
    name, sess = app.resolve(a.session)
    rp = remote_path(a.path, sess.get("cwd", REMOTE_CWD))
    exists = app.helper(name, sess, py_wrap(f"emit({{'e': os.path.isfile({rp!r})}})"), "Checking…").get("e")
    tmpdir = Path(tempfile.mkdtemp(prefix="colab-edit-"))
    local = tmpdir / Path(rp).name
    if exists:
        download_file(app, name, sess, rp, local, quiet=True)
    else:
        info(f"{rp} does not exist yet - creating a new file.")
        local.write_text("", encoding="utf-8")
    before = hashlib.md5(local.read_bytes()).hexdigest()
    info("Opening your editor - save & close it to upload.")
    _open_editor(local)
    if hashlib.md5(local.read_bytes()).hexdigest() == before:
        info("No changes - nothing uploaded.")
    else:
        upload_file(app, name, sess, local, rp)
    shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Commands — Automation & utilities
# --------------------------------------------------------------------------- #
def cmd_auth(app: App, a):
    action = a.action or "status"
    if a.project:
        app.cfg.set("project_id", a.project)
        app.backend._project = a.project
        ok(f"Default project set to [bold]{a.project}[/]")
    if getattr(a, "auth", None):
        app.cfg.set("auth", a.auth)
    cc = getattr(a, "client_oauth_config", None)
    if cc:
        app.cfg.set("client_oauth_config", str(Path(cc).resolve()))

    if action == "login":
        if app.auth.mode == "oauth2":
            app.auth.oauth_login()
        else:
            gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
            if not gcloud:
                raise ColabError("gcloud CLI not found (needed for ADC login).",
                                 "Install Google Cloud SDK, or use:  colab --auth oauth2 -c client.json auth login")
            rc = subprocess.call([gcloud, "auth", "application-default", "login",
                                  f"--scopes={','.join(LOGIN_SCOPES)}"])
            if rc:
                raise ColabError("gcloud login failed.")
            proj = app.cfg.get("project_id")
            if proj:
                subprocess.call([gcloud, "auth", "application-default", "set-quota-project", proj])
        ok("Logged in.")
        action = "status"
    if action == "logout":
        app.cfg.token_path.unlink(missing_ok=True)
        ok("Cached OAuth token removed. (For ADC run:  gcloud auth application-default revoke)")
        return
    if action == "remote":
        name, sess = app.resolve(a.session)
        tok = app.auth.token()
        proj = app.backend.project
        p = app.helper(name, sess, py_wrap(f"""
tok, proj = {tok!r}, {proj!r}
open('/root/.colab_cli_token', 'w').write(tok); os.chmod('/root/.colab_cli_token', 0o600)
os.environ['CLOUDSDK_AUTH_ACCESS_TOKEN_FILE'] = '/root/.colab_cli_token'
os.environ['GOOGLE_CLOUD_PROJECT'] = proj
os.environ['CLOUDSDK_CORE_PROJECT'] = proj
import google.auth, google.oauth2.credentials as _gc
_cred = _gc.Credentials(tok)
google.auth.default = lambda *a, **k: (_cred, proj)
emit({{'ok': True}})
"""), "Pushing credentials to the VM…")
        if p.get("ok"):
            ok(f"GCP auth active in [bold]{name}[/] (project {proj}). BigQuery / Cloud Storage clients and "
               "gcloud now work in this kernel.")
            console.print(f"[{MUTED}]The access token lasts ~1 hour; re-run  colab auth remote  to refresh.[/]")
        return
    # status
    with console.status(f"[{ORANGE}]Checking credentials…"):
        acct = app.auth.account()
        try:
            proj = app.backend.project
        except ColabError:
            proj = "[red]not set → colab auth --set-project ID[/]"
        try:
            n_tools = len(app.backend.tools())
            api = f"[green]reachable[/] ({n_tools} tools)"
        except ColabError as e:
            api = f"[red]error[/] {str(e).splitlines()[0]}"
        scope_txt = "unknown"
        try:
            r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                             params={"access_token": app.auth.token()}, timeout=15).json()
            granted = r.get("scope", "")
            scope_txt = ("[green]colaboratory ✔[/]" if "auth/colaboratory" in granted
                         else "[red]colaboratory MISSING → run  colab auth login[/]")
        except Exception:
            pass
    tb = Table(box=None, show_header=False, padding=(0, 2))
    tb.add_column(style=f"bold {ORANGE}")
    tb.add_column()
    for k, v in [("Account", acct), ("Strategy", app.auth.mode), ("Project", proj),
                 ("Colab API", api), ("Colab scope", scope_txt), ("Config", Text(str(app.cfg.path)))]:
        tb.add_row(k, v)
    console.print(Panel(tb, title="[bold]Authentication", border_style=ORANGE, box=box.ROUNDED, expand=False))


def cmd_drivemount(app: App, a):
    name, sess = app.resolve(a.session)
    hint = ("Drive mounting needs interactive consent inside Colab. If this runtime cannot show it, use "
            "colab auth remote  and access Drive files via the Drive API / gcloud storage instead.")
    try:
        p = app.helper(name, sess, py_wrap(f"""
mp = {a.mountpoint!r}
if os.path.ismount(mp): emit({{'ok': True, 'already': True}})
else:
    from google.colab import drive
    drive.mount(mp, force_remount={bool(a.force)!r})
    emit({{'ok': os.path.ismount(mp)}})
"""), "Mounting Google Drive…", timeout=300)
    except ColabError as e:
        raise ColabError(str(e), hint) from e
    if p.get("ok"):
        ok(f"Google Drive {'already ' if p.get('already') else ''}mounted at [bold]{a.mountpoint}[/]")
    else:
        raise ColabError("Drive mount did not complete.", hint)


def cmd_install(app: App, a):
    if not a.packages and not a.requirements:
        raise ColabError("No packages given.", "Example:  colab install transformers==4.44 accelerate")
    name, sess = app.resolve(a.session)
    parts = [shlex.quote(p) for p in a.packages]
    if a.requirements:
        rp = Path(a.requirements)
        if rp.is_file():
            upload_file(app, name, sess, rp, "/tmp/colabcli-requirements.txt", quiet=True)
            parts += ["-r", "/tmp/colabcli-requirements.txt"]
        else:
            parts += ["-r", shlex.quote(a.requirements)]
    if a.upgrade:
        parts.insert(0, "-U")
    pk = " ".join(parts)
    cmd = f"if command -v uv >/dev/null 2>&1; then uv pip install --system {pk}; " \
          f"else python -m pip install -q {pk}; fi" if not a.pip else f"python -m pip install {pk}"
    res, rc, _ = remote_shell(app, name, sess, cmd, spinner=f"Installing {' '.join(a.packages)[:40]}…")
    res.render(show_result=False)
    if rc:
        raise ColabError(f"Install failed (exit {rc}).")
    ok("Packages installed. [dim]Restart the kernel if you upgraded an already-imported package.[/]")


def _load_log(app, name) -> list[dict]:
    p = app.cfg.logs_dir / f"{name}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def cmd_log(app: App, a):
    if a.session:
        name = a.session
    else:
        try:
            name, _ = app.resolve(None)
        except ColabError:
            logs = sorted(app.cfg.logs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) \
                if app.cfg.logs_dir.exists() else []
            if not logs:
                raise
            name = logs[-1].stem
            info(f"No live session - showing log of past session [bold]{name}[/]")
    entries = _load_log(app, name)
    if not entries:
        info(f"No log entries for {name} yet.")
        return
    if a.export:
        out = Path(a.export)
        ext = out.suffix.lower()
        if ext == ".ipynb":
            atomic_write(out, json.dumps(build_notebook(entries), indent=1))
        elif ext == ".jsonl":
            atomic_write(out, "\n".join(json.dumps(e) for e in entries) + "\n")
        elif ext == ".md":
            md = [f"# Colab session log — {name}\n"]
            for e in entries:
                md.append(f"## {e['ts']} · {e['kind']} · {e.get('seconds', 0)}s\n\n```python\n{e['code']}\n```\n")
                if e.get("stdout"):
                    md.append(f"```text\n{e['stdout']}\n```\n")
                if e.get("error"):
                    md.append(f"**Error**\n\n```text\n{e['error']}\n```\n")
            atomic_write(out, "\n".join(md))
        elif ext == ".txt":
            txt = []
            for e in entries:
                txt.append(f"[{e['ts']}] ({e['kind']}) >>>\n{e['code']}\n---\n{e.get('stdout', '')}"
                           + (f"\nERROR:\n{e['error']}" if e.get("error") else "") + "\n")
            atomic_write(out, "\n".join(txt))
        else:
            raise ColabError("Unsupported export format.", "Use .ipynb, .md, .txt or .jsonl")
        ok(f"Exported {len(entries)} entries → [bold]{out}[/]")
        return
    for e in entries[-a.n:]:
        status = "[red]✘[/]" if e.get("error") else "[green]✔[/]"
        console.print(f"{status} [{MUTED}]{e['ts'].replace('T', ' ')} · {e['kind']} · {e.get('seconds', 0)}s[/]")
        console.print(Syntax(e["code"][:2000], "python", theme="monokai", background_color="default",
                             line_numbers=False))
        if e.get("stdout"):
            console.print(Text(e["stdout"][:2000].rstrip(), style="white"))
        if e.get("error"):
            console.print(Text(e["error"][:1500], style="red"))
        console.rule(style="grey23")


def cmd_usage(app: App, a):
    tool = app.backend.find_tool("get_usage", "get_compute_units", "get_user_info", "get_subscription",
                                 contains=("usage",)) or \
        app.backend.find_tool(contains=("compute",)) or app.backend.find_tool(contains=("quota",)) or \
        app.backend.find_tool(contains=("subscription",))
    if tool:
        with console.status(f"[{ORANGE}]Fetching usage…"):
            data = app.backend.call(tool, {})
        tb = Table(box=box.ROUNDED, border_style=MUTED, header_style=f"bold {ORANGE}", title="Colab usage")
        tb.add_column("Field")
        tb.add_column("Value")

        def flat(o, pre=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    flat(v, f"{pre}{k}.")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    flat(v, f"{pre}{i}.")
            else:
                tb.add_row(pre.rstrip("."), Text(str(o)))
        flat(data)
        console.print(tb)
    else:
        info("The Colab MCP backend exposes no usage/compute-unit tool for your account.")
    total = 0.0
    for s in app.store.data.values():
        try:
            total += (dt.datetime.now() - dt.datetime.fromisoformat(s["created"])).total_seconds()
        except Exception:
            pass
    console.print(f"[{MUTED}]Local view: {len(app.store.data)} active session(s), "
                  f"~{total / 3600:.1f} runtime-hours since creation. "
                  "Exact compute-unit balance:  colab pay[/]")


def cmd_pay(app: App, a):
    console.print(Panel(f"Manage plans & compute units:\n[bold]{COLAB_PAY}[/]",
                        border_style=ORANGE, box=box.ROUNDED, expand=False))
    if not a.no_open:
        webbrowser.open(COLAB_PAY)
        ok("Opened in your browser.")


def cmd_version(app: App, a):
    import google.auth as ga
    try:
        import prompt_toolkit as ptk
        ptv = ptk.__version__
    except ImportError:
        ptv = "not installed (repl uses basic mode)"
    tb = Table(box=None, show_header=False, padding=(0, 2))
    tb.add_column(style=f"bold {ORANGE}")
    tb.add_column()
    for k, v in [("colab-cli", __version__), ("Python", sys.version.split()[0]),
                 ("OS", platform.platform()), ("google-auth", getattr(ga, "__version__", "?")),
                 ("prompt_toolkit", ptv), ("Installed at", __file__), ("Config dir", str(app.cfg.dir))]:
        tb.add_row(k, Text(v))
    console.print(Panel(tb, title="[bold]Version", border_style=ORANGE, box=box.ROUNDED, expand=False))


def cmd_update(app: App, a):
    src = a.source or app.cfg.get("update_source")
    if a.deps or not src:
        info("Upgrading dependencies…")
        subprocess.call([sys.executable, "-m", "pip", "install", "-U", "-q", "rich", "requests",
                         "google-auth", "google-auth-oauthlib", "prompt_toolkit", "pygments"])
        ok("Dependencies up to date.")
    if not src:
        console.print(f"[{MUTED}]No update source configured. Use  colab update --source URL_OR_PATH  "
                      "(it is remembered for next time).[/]")
        return
    with console.status(f"[{ORANGE}]Fetching {src}…"):
        if re.match(r"https?://", src):
            r = requests.get(src, timeout=60)
            r.raise_for_status()
            new = r.text
        else:
            new = Path(src).read_text(encoding="utf-8")
    try:
        compile(new, "colab_cli.py", "exec")
    except SyntaxError as e:
        raise ColabError(f"Downloaded file is not valid Python: {e}")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', new)
    newv = m.group(1) if m else "?"
    me = Path(__file__).resolve()
    if new == me.read_text(encoding="utf-8"):
        ok(f"Already up to date (v{__version__}).")
        return
    shutil.copy2(me, me.with_suffix(".py.bak"))
    atomic_write(me, new)
    app.cfg.set("update_source", src)
    ok(f"Updated v{__version__} → v{newv}  (backup: {me.with_suffix('.py.bak').name})")


def cmd_tools(app: App, a):
    """Hidden debug command: list backend MCP tools."""
    tb = Table(box=box.ROUNDED, border_style=MUTED, header_style=f"bold {ORANGE}", title="Backend MCP tools")
    tb.add_column("Tool")
    tb.add_column("Arguments", style=MUTED)
    tb.add_column("Description")
    for n, t in sorted(app.backend.tools().items()):
        props = ", ".join((t.get("inputSchema") or {}).get("properties", {}).keys())
        tb.add_row(n, props, Text((t.get("description") or "")[:90]))
    console.print(tb)


# --------------------------------------------------------------------------- #
#  Argument parsing
# --------------------------------------------------------------------------- #
def build_parser():
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--auth", choices=["adc", "oauth2"], default=argparse.SUPPRESS)
    g.add_argument("-c", "--client-oauth-config", default=argparse.SUPPRESS)
    g.add_argument("--config", default=argparse.SUPPRESS)
    g.add_argument("--project", default=argparse.SUPPRESS)
    g.add_argument("--logtostderr", action="store_true", default=argparse.SUPPRESS)

    # sub-commands get the same global flags, but without the short -c
    # (so `colab exec -c CODE` works like python -c)
    gs = argparse.ArgumentParser(add_help=False)
    gs.add_argument("--auth", choices=["adc", "oauth2"], default=argparse.SUPPRESS)
    gs.add_argument("--client-oauth-config", default=argparse.SUPPRESS)
    gs.add_argument("--config", default=argparse.SUPPRESS)
    gs.add_argument("--project", default=argparse.SUPPRESS)
    gs.add_argument("--logtostderr", action="store_true", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(prog="colab", parents=[g], add_help=False)
    p.add_argument("-h", "--help", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, help_, session=True, **kw):
        sp = sub.add_parser(name, parents=[gs], help=help_,
                            description=None if help_ == argparse.SUPPRESS else help_, **kw)
        if session:
            sp.add_argument("-s", "--session", metavar="NAME", help="session name (optional if only one)")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("new", cmd_new, "Allocate a new CPU/GPU/TPU runtime")
    sp.add_argument("--gpu", metavar="GPU", help="T4, L4, G4, A100, H100 …")
    sp.add_argument("--tpu", metavar="TPU", help="v5e1, v6e1 …")
    sp.add_argument("--high-mem", action="store_true", help="high-RAM machine shape")
    sp.add_argument("--list", action="store_true", help="list available runtime specs and exit")

    add("sessions", cmd_sessions, "List active sessions", session=False)
    sp = add("status", cmd_status, "Show hardware & runtime status")
    sp.add_argument("--no-probe", action="store_true", help="skip the live hardware probe")
    add("restart-kernel", cmd_restart_kernel, "Restart the Jupyter kernel")
    sp = add("stop", cmd_stop, "Terminate a session VM")
    sp.add_argument("--all", action="store_true", help="stop every session")
    sp.add_argument("-y", "--yes", action="store_true", help="no confirmation")
    sp = add("url", cmd_url, "Print/open a browser URL for the session")
    sp.add_argument("--open", action="store_true")

    sp = add("run", cmd_run, "Run a script/command on a fresh VM, then tear it down")
    sp.add_argument("--gpu")
    sp.add_argument("--tpu")
    sp.add_argument("--high-mem", action="store_true")
    sp.add_argument("--keep", action="store_true", help="keep the VM after the run")
    sp.add_argument("--with", dest="with_files", action="append", metavar="PATH",
                    help="extra local file/folder to upload first (repeatable)")
    sp.add_argument("--download", action="append", metavar="REMOTE", help="fetch this remote path afterwards")
    sp.add_argument("target", nargs=argparse.REMAINDER, help="script.py|nb.ipynb|script.sh|shell command [args]")

    sp = add("exec", cmd_exec, "Execute Python from stdin, .py or .ipynb")
    sp.add_argument("file", nargs="?", help=".py / .ipynb file, or - for stdin")
    sp.add_argument("-c", "--code", help="inline Python code")
    sp.add_argument("--save", metavar="OUT.ipynb", help="save executed notebook with outputs")
    sp.add_argument("--timeout", type=int, help="seconds to wait (default 660)")

    add("repl", cmd_repl, "Interactive multi-line Python REPL on the VM")
    sp = add("ssh", cmd_ssh, "Remote shell / one-shot command")
    sp.add_argument("--proxy", action="store_true", help="OpenSSH ProxyCommand mode (not supported)")
    sp.add_argument("--timeout", type=int)
    sp.add_argument("command", nargs=argparse.REMAINDER)

    sp = add("ls", cmd_ls, "List remote files")
    sp.add_argument("path", nargs="?", default="")
    sp.add_argument("-l", "--long", action="store_true")
    sp.add_argument("-a", "--all", action="store_true")
    sp = add("upload", cmd_upload, "Upload local files/folders")
    sp.add_argument("local", nargs="+")
    sp.add_argument("-d", "--dest", dest="remote", help="remote destination (default: cwd)")
    sp = add("download", cmd_download, "Download remote files/folders")
    sp.add_argument("remote")
    sp.add_argument("local", nargs="?")
    sp = add("rm", cmd_rm, "Delete remote files")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("-r", "--recursive", action="store_true")
    sp.add_argument("-f", "--force", action="store_true", help="no confirmation")
    sp = add("edit", cmd_edit, "Edit a remote file with your local $EDITOR")
    sp.add_argument("path")

    sp = add("auth", cmd_auth, "Login / status / push GCP auth into the VM")
    sp.add_argument("action", nargs="?", choices=["login", "status", "logout", "remote"])
    sp.add_argument("--set-project", dest="project", help="save default GCP project")
    sp = add("drivemount", cmd_drivemount, "Mount Google Drive")
    sp.add_argument("--mountpoint", default="/content/drive")
    sp.add_argument("--force", action="store_true", help="force remount")
    sp = add("install", cmd_install, "Install Python packages (uv/pip)")
    sp.add_argument("packages", nargs="*")
    sp.add_argument("-r", "--requirements", help="local or remote requirements.txt")
    sp.add_argument("-U", "--upgrade", action="store_true")
    sp.add_argument("--pip", action="store_true", help="force pip instead of uv")
    sp = add("log", cmd_log, "View/export session logs")
    sp.add_argument("--export", metavar="FILE", help=".ipynb .md .txt .jsonl")
    sp.add_argument("-n", type=int, default=20, help="entries to show")
    add("usage", cmd_usage, "Compute-unit usage", session=False)
    sp = add("pay", cmd_pay, "Manage subscription", session=False)
    sp.add_argument("--no-open", action="store_true")
    add("version", cmd_version, "Show version", session=False)
    sp = add("update", cmd_update, "Upgrade CLI in place", session=False)
    sp.add_argument("--source", help="URL or local path of the new colab_cli.py")
    sp.add_argument("--deps", action="store_true", help="also upgrade Python dependencies")
    add("tools", cmd_tools, argparse.SUPPRESS, session=False)
    return p


def show_error(e: ColabError):
    body = Text(str(e))
    if getattr(e, "hint", None):
        body.append("\n\n→ ", style=f"bold {ORANGE}")
        body.append(e.hint, style=ORANGE)
    err_console.print(Panel(body, title="[bold red]Error", border_style="red", box=box.ROUNDED, expand=False))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv or argv[0] in ("-h", "--help", "help"):
        if len(argv) > 1 and argv[0] == "help":
            argv = [argv[1], "--help"]
        else:
            print_help()
            return 0
    if argv[0] in ("--version", "-V"):
        argv = ["version"]
    args = parser.parse_args(argv)
    if getattr(args, "help", False) or not getattr(args, "fn", None):
        print_help()
        return 0
    cfg_dir = config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(cfg_dir / "debug.log", encoding="utf-8")]
    if getattr(args, "logtostderr", False):
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        app = App(args)
        args.fn(app, args)
        return 0
    except ColabError as e:
        show_error(e)
        return 1
    except KeyboardInterrupt:
        err_console.print(f"\n[{MUTED}]Interrupted.[/]")
        return 130
    except SystemExit:
        raise
    except Exception as e:  # last-resort safety net
        log.exception("unexpected")
        show_error(ColabError(f"Unexpected {type(e).__name__}: {e}",
                              f"Full traceback saved to {cfg_dir / 'debug.log'} (or rerun with --logtostderr)"))
        if getattr(args, "logtostderr", False):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
