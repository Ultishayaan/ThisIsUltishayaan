"""
This Is Ultishayaan - a zero-dependency dashboard for all local web projects.

Usage:
    python app.py
    python app.py --port 7777
    python app.py --no-browser
"""

import argparse
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
LOGS_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "projects.json")

os.makedirs(LOGS_DIR, exist_ok=True)


# ---------- Config ----------

# `~` is expanded with os.path.expanduser at load time so the config file
# stays portable and does not embed a personal home directory.
DEFAULT_SCAN_ROOT = os.path.expanduser("~/PycharmProjects")

DEFAULT_CONFIG = {
    "scan_root": DEFAULT_SCAN_ROOT,
    "hub_port": 7777,
    "auto_start": False,
    "projects": {},
}


def expand_path(value):
    """Expand ~ and environment variables in a path string."""
    if not isinstance(value, str):
        return value
    return os.path.expandvars(os.path.expanduser(value))


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return _normalize_paths(dict(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        merged["projects"] = data.get("projects", {}) or {}
        return _normalize_paths(merged)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[hub] Could not read projects.json ({exc}); using defaults", file=sys.stderr)
        return _normalize_paths(dict(DEFAULT_CONFIG))


def _normalize_paths(cfg):
    """Expand ~ and env vars in any user-supplied paths."""
    if "scan_root" in cfg:
        cfg["scan_root"] = expand_path(cfg["scan_root"])
    for proj in cfg.get("projects", {}).values():
        for key in ("path", "cwd", "entry"):
            if proj.get(key):
                proj[key] = expand_path(proj[key])
        if isinstance(proj.get("env"), dict):
            proj["env"] = {k: expand_path(v) if isinstance(v, str) else v for k, v in proj["env"].items()}
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------- Project detection ----------

WEB_INDICATORS = (
    "package.json",
    "index.html",
    "app.py",
    "main.py",
    "requirements.txt",
)

NODE_WEB_DEPS = {
    "express",
    "fastify",
    "koa",
    "socket.io",
    "next",
    "nuxt",
    "vite",
    "react-scripts",
    "astro",
    "svelte",
    "remix",
    "gatsby",
    "http-server",
    "serve",
    "polka",
    "hapi",
}

PYTHON_WEB_DEPS = {
    "flask",
    "fastapi",
    "django",
    "starlette",
    "sanic",
    "tornado",
    "bottle",
    "aiohttp",
}

EXCLUDE_DIRS = {
    "node_modules",
    ".git",
    ".idea",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    "target",
    "out",
    ".next",
    ".nuxt",
    ".cache",
    ".parcel-cache",
    "coverage",
    ".gradle",
    "captures",
    ".expo",
    ".vercel",
    "static",
    "public",
    "assets",
    "www",
    "html",
    "resources",
    "templates",
}


def has_marker(path, names):
    return any(os.path.exists(os.path.join(path, n)) for n in names)


def read_package_json(path):
    pkg = os.path.join(path, "package.json")
    if not os.path.exists(pkg):
        return None
    try:
        with open(pkg, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def read_requirements(path):
    req = os.path.join(path, "requirements.txt")
    if not os.path.exists(req):
        return None
    try:
        with open(req, "r", encoding="utf-8") as f:
            return f.read().lower()
    except OSError:
        return None


def find_web_root(path):
    """Return the most likely subdirectory that holds the web entry, or path itself."""
    # Root has a package.json with web deps -> use root
    pkg = read_package_json(path)
    if pkg:
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        dep_names = {d.split("/")[0].lower() for d in deps}
        if dep_names & NODE_WEB_DEPS:
            return path
    # Look one level deep for backend/frontend folders
    try:
        for entry in sorted(os.listdir(path)):
            sub = os.path.join(path, entry)
            if not os.path.isdir(sub) or entry in EXCLUDE_DIRS or entry.startswith("."):
                continue
            sub_pkg = read_package_json(sub)
            if sub_pkg:
                deps = {**sub_pkg.get("dependencies", {}), **sub_pkg.get("devDependencies", {})}
                dep_names = {d.split("/")[0].lower() for d in deps}
                if dep_names & NODE_WEB_DEPS:
                    return sub
            if os.path.exists(os.path.join(sub, "index.html")):
                return sub
    except OSError:
        pass
    return None


def looks_like_web_project(path):
    return find_web_root(path) is not None


def detect_projects(scan_root):
    if not os.path.isdir(scan_root):
        return []
    projects = []
    for entry in sorted(os.listdir(scan_root)):
        full = os.path.join(scan_root, entry)
        if not os.path.isdir(full):
            continue
        if entry in EXCLUDE_DIRS or entry.startswith("."):
            continue
        web_root = find_web_root(full)
        if web_root:
            cwd = web_root
            info = detect_project(cwd, entry)
            if info:
                info["path"] = full
                info["cwd"] = cwd
                if info.get("command"):
                    info["command"] = info["command"]  # keep
                projects.append(info)
    return projects


def detect_project(path, name):
    info = {
        "name": name,
        "path": path,
        "type": "unknown",
        "description": "",
        "command": None,
        "cwd": path,
        "port": None,
        "url": None,
        "entry": None,
        "tags": [],
    }

    pkg = read_package_json(path)
    if pkg:
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        dep_names = {d.split("/")[0].lower() for d in deps}
        scripts = pkg.get("scripts", {}) or {}
        info["description"] = pkg.get("description", "") or ""
        info["tags"] = list(dep_names & NODE_WEB_DEPS)

        start_script = scripts.get("start") or scripts.get("dev") or scripts.get("serve")
        if "next" in dep_names:
            info["type"] = "nextjs"
            info["command"] = "npm run dev" if "dev" in scripts else "npm start"
            info["port"] = 3000
        elif "vite" in dep_names:
            info["type"] = "vite"
            info["command"] = "npm run dev" if "dev" in scripts else "npm start"
            info["port"] = 5173
        elif "nuxt" in dep_names:
            info["type"] = "nuxt"
            info["command"] = "npm run dev" if "dev" in scripts else "npm start"
            info["port"] = 3000
        elif dep_names & NODE_WEB_DEPS:
            info["type"] = "node"
            info["command"] = f"npm start" if "start" in scripts else (
                f"npm run {list(scripts.keys())[0]}" if scripts else "npm start"
            )
            info["port"] = 3000
        elif scripts:
            info["type"] = "node"
            info["command"] = f"npm start" if "start" in scripts else f"npm run {list(scripts.keys())[0]}"
            info["port"] = 3000
        else:
            info["type"] = "node"
            info["command"] = "npm start"
            info["port"] = 3000

        if start_script and "PORT" not in (start_script or ""):
            port_match = re.search(r"(?:PORT[=:\s]+|port[=:\s]+)(\d+)", start_script)
            if not port_match:
                port_match = re.search(r"--port[= ]+(\d+)", start_script)
            if not port_match:
                port_match = re.search(r"-p\s*(\d+)", start_script)
            if port_match:
                info["port"] = int(port_match.group(1))

    req = read_requirements(path)
    if req and any(dep in req for dep in PYTHON_WEB_DEPS):
        if "flask" in req:
            info["type"] = info.get("type", "flask") or "flask"
            info["command"] = info.get("command") or "python app.py"
            info["port"] = info.get("port") or 5000
        elif "fastapi" in req or "starlette" in req or "uvicorn" in req:
            info["type"] = info.get("type", "fastapi") or "fastapi"
            info["command"] = info.get("command") or "uvicorn main:app --reload"
            info["port"] = info.get("port") or 8000
        elif "django" in req:
            info["type"] = "django"
            info["command"] = "python manage.py runserver"
            info["port"] = 8000

    for cand in ("public/index.html", "frontend/index.html", "index.html"):
        if os.path.exists(os.path.join(path, cand)):
            info["entry"] = cand
            if not info.get("url"):
                url_path = "/" + cand.replace(os.sep, "/")
                if info.get("port"):
                    info["url"] = f"http://localhost:{info['port']}{url_path}"
            break

    if info.get("port") and not info.get("url"):
        info["url"] = f"http://localhost:{info['port']}/"

    return info


# ---------- Process manager ----------

class ProjectRunner:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.process = None
        self.log_path = os.path.join(LOGS_DIR, f"{self._safe_name()}.log")
        self.log_buffer = deque(maxlen=2000)
        self.lock = threading.RLock()  # reentrant: helpers call is_running() from within start()
        self.log_lock = threading.Lock()
        self.started_at = None
        self.last_error = None
        self.subscribers = []
        self.sub_lock = threading.Lock()
        self._stopping = False

    def _safe_name(self):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", self.name)

    def is_running(self):
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def status(self):
        running = self.is_running()
        return {
            "name": self.name,
            "running": running,
            "pid": self.process.pid if running else None,
            "started_at": self.started_at,
            "uptime": (time.time() - self.started_at) if (running and self.started_at) else 0,
            "port": self.cfg.get("port"),
            "url": self.cfg.get("url"),
            "log_file": os.path.relpath(self.log_path, APP_DIR),
            "last_error": self.last_error,
            "command": self.cfg.get("command"),
            "cwd": self.cfg.get("cwd"),
        }

    def start(self, install_deps=False):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return {"ok": True, "already": True, "status": self.status()}

            command = self.cfg.get("command")
            cwd = self.cfg.get("cwd") or self.cfg.get("path")
            if not command or not cwd or not os.path.isdir(cwd):
                self.last_error = f"Invalid config: command={command!r} cwd={cwd!r}"
                return {"ok": False, "error": self.last_error}

            env = os.environ.copy()
            env.update(self.cfg.get("env", {}) or {})
            env["FORCE_COLOR"] = "0"

            if install_deps and command.lower().startswith("npm "):
                self._run_step("npm install" if sys.platform == "win32" else ["npm", "install"], cwd, env)

            try:
                self._stopping = False
                popen_kwargs = dict(
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if sys.platform == "win32":
                    popen_kwargs["shell"] = True
                    popen_kwargs["args"] = command
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    popen_kwargs["args"] = self._split_command(command)
                self.process = subprocess.Popen(**popen_kwargs)
            except (OSError, ValueError) as exc:
                self.last_error = f"Failed to start: {exc}"
                self._append_log(f"[hub] {self.last_error}\n")
                return {"ok": False, "error": self.last_error}

            self.started_at = time.time()
            self.last_error = None
            self._append_log(
                f"[hub] started '{self.name}' (pid={self.process.pid}) "
                f"cwd={cwd} cmd={command}\n"
            )
            threading.Thread(target=self._pump_output, daemon=True).start()
            return {"ok": True, "status": self.status()}

    def stop(self, timeout=8):
        with self.lock:
            proc = self.process
            if not proc or proc.poll() is not None:
                self.process = None
                return {"ok": True, "already_stopped": True, "status": self.status()}

            self._stopping = True
            self._append_log(f"[hub] stopping '{self.name}' (pid={proc.pid})...\n")

            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    proc.terminate()
            except OSError as exc:
                self._append_log(f"[hub] stop error: {exc}\n")

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._append_log(f"[hub] force-killing '{self.name}'\n")
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception as exc:
                    self._append_log(f"[hub] kill error: {exc}\n")

            self.process = None
            self.started_at = None
            self._append_log(f"[hub] stopped '{self.name}'\n")
            return {"ok": True, "status": self.status()}

    def tail_log(self, n=200):
        with self.log_lock:
            return "".join(list(self.log_buffer)[-n:])

    def read_full_log(self):
        if not os.path.exists(self.log_path):
            return ""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as exc:
            return f"(could not read log: {exc})"

    _log_lock = threading.Lock()

    def _append_log(self, text):
        with self.log_lock:
            self.log_buffer.append(text)
        try:
            with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except OSError:
            pass
        with self.sub_lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(text)
            except queue.Full:
                pass

    def _pump_output(self):
        proc = self.process
        if not proc or not proc.stdout:
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                self._append_log(line if line.endswith("\n") else line + "\n")
        except (OSError, ValueError):
            pass
        finally:
            try:
                code = proc.poll()
            except Exception:
                code = None
            self._append_log(f"[hub] process exited (code={code})\n")
            if not self._stopping and code not in (0, None):
                self.last_error = f"Process exited with code {code}"
            with self.lock:
                self.process = None
                self.started_at = None

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.sub_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    @staticmethod
    def _split_command(cmd):
        try:
            import shlex
            return shlex.split(cmd, posix=False)
        except ValueError:
            return cmd.split()

    def _run_step(self, args, cwd, env):
        """Run a setup step (e.g. npm install) and stream its output to the log."""
        try:
            kwargs = dict(
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if sys.platform == "win32":
                kwargs["args"] = " ".join(args) if isinstance(args, list) else args
                kwargs["shell"] = True
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["args"] = args if isinstance(args, list) else args.split()
            self._append_log(f"[hub] running setup: {kwargs.get('args')}\n")
            proc = subprocess.Popen(**kwargs)
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                self._append_log(line if line.endswith("\n") else line + "\n")
            proc.wait(timeout=900)
        except Exception as exc:
            self._append_log(f"[hub] setup error: {exc}\n")


# Hack: a single global lock for the log buffer (the inner one is per-instance).
_log_lock_dummy = threading.Lock()


# ---------- HTTP server ----------

class HubState:
    def __init__(self):
        self.config = load_config()
        self.runners = {}
        self.lock = threading.Lock()
        self._spawn_runners()
        self.scan_thread = None
        self.last_scan = []

    def _spawn_runners(self):
        for name, cfg in self.config.get("projects", {}).items():
            if not cfg.get("enabled", True):
                continue
            self.runners[name] = ProjectRunner(name, cfg)

    def reload(self):
        with self.lock:
            for name, runner in list(self.runners.items()):
                if runner.is_running():
                    continue
            self.config = load_config()
            existing = set(self.runners)
            for name, cfg in self.config.get("projects", {}).items():
                if not cfg.get("enabled", True):
                    continue
                if name in existing:
                    self.runners[name].cfg = cfg
                else:
                    self.runners[name] = ProjectRunner(name, cfg)
            for name in list(self.runners):
                if name not in self.config.get("projects", {}):
                    if not self.runners[name].is_running():
                        del self.runners[name]
        return self.snapshot()

    def snapshot(self):
        projects = []
        for name, cfg in self.config.get("projects", {}).items():
            runner = self.runners.get(name)
            if runner is None:
                if not cfg.get("enabled", True):
                    continue
                runner = ProjectRunner(name, cfg)
                self.runners[name] = runner
            entry = dict(cfg)
            entry["name"] = name
            entry["status"] = runner.status()
            projects.append(entry)
        return {
            "hub": {
                "port": self.config.get("hub_port"),
                "scan_root": self.config.get("scan_root"),
                "auto_start": self.config.get("auto_start", False),
            },
            "discovered": self.last_scan,
            "projects": projects,
        }

    def scan(self):
        def worker():
            root = self.config.get("scan_root")
            found = detect_projects(root)
            with self.lock:
                self.last_scan = found
        if self.scan_thread and self.scan_thread.is_alive():
            return
        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()


STATE = HubState()
STATE.scan()


# ---------- HTTP handler ----------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def send_json(handler, payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def send_text(handler, text, status=200, content_type="text/plain; charset=utf-8"):
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def send_redirect(handler, location):
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def serve_static(handler, rel_path):
    rel_path = rel_path.lstrip("/")
    if not rel_path or rel_path in ("", "/"):
        rel_path = "index.html"
    full = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
    if not full.startswith(os.path.normpath(STATIC_DIR)):
        return send_text(handler, "Forbidden", 403)
    if not os.path.isfile(full):
        return send_text(handler, "Not Found", 404)
    ext = os.path.splitext(full)[1].lower()
    ctype = MIME.get(ext, "application/octet-stream")
    try:
        with open(full, "rb") as f:
            data = f.read()
    except OSError as exc:
        return send_text(handler, f"Read error: {exc}", 500)
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


class HubHandler(BaseHTTPRequestHandler):
    server_version = "UltishayaanHub/1.0"

    # Silence default access logs (we use a custom one).
    def log_message(self, format, *args):
        try:
            sys.stderr.write("[hub] %s - %s\n" % (self.address_string(), format % args))
        except Exception:
            pass

    # ---- routing ----

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return serve_static(self, "index.html")
        if path.startswith("/static/"):
            return serve_static(self, path[len("/static/"):])
        if path == "/api/projects":
            return self.api_projects()
        if path == "/api/scan":
            STATE.scan()
            return send_json(self, {"ok": True, "queued": True})
        if path == "/api/discovered":
            return send_json(self, {"discovered": STATE.last_scan})
        if path == "/api/config":
            return send_json(self, STATE.config)
        if path.startswith("/api/projects/") and path.endswith("/status"):
            return self.api_status(path)
        if path.startswith("/api/projects/") and path.endswith("/log"):
            return self.api_log(path)
        if path.startswith("/api/projects/") and path.endswith("/log/stream"):
            return self.api_log_stream(path)
        if path.startswith("/api/projects/") and "/start" in path:
            return self.api_start(path)
        if path.startswith("/api/projects/") and "/stop" in path:
            return self.api_stop(path)
        if path.startswith("/api/projects/") and path.endswith("/open"):
            return self.api_open(path)
        if path == "/api/health":
            return send_json(self, {"ok": True, "time": time.time()})
        return send_text(self, "Not Found", 404)

    def do_POST(self):
        cl = int(self.headers.get("Content-Length", "0") or 0)
        if cl > 0:
            self.rfile.read(cl)
        return self.do_GET()

    # ---- API ----

    def _project_name(self, path, suffix):
        prefix = "/api/projects/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        if not rest.endswith(suffix):
            return None
        return rest[: -len(suffix)].rstrip("/")

    def api_projects(self):
        return send_json(self, STATE.snapshot())

    def api_status(self, path):
        name = self._project_name(path, "/status")
        runner = STATE.runners.get(name) if name else None
        if not runner:
            return send_json(self, {"ok": False, "error": "unknown project"}, 404)
        return send_json(self, {"ok": True, "status": runner.status()})

    def api_log(self, path):
        name = self._project_name(path, "/log")
        runner = STATE.runners.get(name) if name else None
        if not runner:
            return send_json(self, {"ok": False, "error": "unknown project"}, 404)
        qs = urlparse(self.path).query
        n = 400
        if "n=" in qs:
            try:
                n = max(1, min(5000, int(qs.split("n=")[1].split("&")[0])))
            except ValueError:
                pass
        return send_json(self, {"ok": True, "log": runner.tail_log(n)})

    def api_log_stream(self, path):
        name = self._project_name(path, "/log/stream")
        runner = STATE.runners.get(name) if name else None
        if not runner:
            return send_json(self, {"ok": False, "error": "unknown project"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = runner.subscribe()
        try:
            # Send buffered tail first.
            self.wfile.write(f"event: hello\ndata: {json.dumps({'name': name})}\n\n".encode())
            self.wfile.flush()
            for line in runner.tail_log(200).splitlines(keepends=True):
                self.wfile.write(b"data: " + line.rstrip("\n").encode("utf-8", "replace") + b"\n")
            self.wfile.write(b"\n")
            self.wfile.flush()

            last_ping = time.time()
            while True:
                try:
                    chunk = q.get(timeout=15)
                    if chunk:
                        for line in chunk.splitlines(keepends=True):
                            self.wfile.write(b"data: " + line.rstrip("\n").encode("utf-8", "replace") + b"\n")
                        self.wfile.write(b"\n")
                        self.wfile.flush()
                except queue.Empty:
                    pass
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            runner.unsubscribe(q)

    def api_start(self, path):
        name = self._project_name(path, "/start")
        runner = STATE.runners.get(name) if name else None
        if not runner:
            return send_json(self, {"ok": False, "error": "unknown project"}, 404)
        install = "install=1" in (urlparse(self.path).query or "")
        result = runner.start(install_deps=install)
        status = 200 if result.get("ok") else 500
        return send_json(self, result, status=status)

    def api_stop(self, path):
        name = self._project_name(path, "/stop")
        runner = STATE.runners.get(name) if name else None
        if not runner:
            return send_json(self, {"ok": False, "error": "unknown project"}, 404)
        result = runner.stop()
        return send_json(self, result)

    def api_open(self, path):
        name = self._project_name(path, "/open")
        runner = STATE.runners.get(name) if name else None
        if not runner:
            return send_json(self, {"ok": False, "error": "unknown project"}, 404)
        if not runner.is_running():
            res = runner.start()
            if not res.get("ok"):
                return send_json(self, res, 500)
        cfg = STATE.config.get("projects", {}).get(name, {})
        url = cfg.get("url")
        if not url:
            return send_json(self, {"ok": False, "error": "no url configured"}, 400)
        return send_json(self, {"ok": True, "url": url, "status": runner.status()})


# ---------- Server bootstrap ----------

class ThreadedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_free_port(preferred):
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", preferred))
        return preferred
    except OSError:
        s.close()
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
        s.close()
        return port


def main():
    parser = argparse.ArgumentParser(description="This Is Ultishayaan - local projects dashboard")
    parser.add_argument("--port", type=int, default=None, help="Port for the hub UI (default from projects.json or 7777)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default 0.0.0.0)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the dashboard")
    parser.add_argument("--scan-root", default=None, help="Override the scan root directory")
    args = parser.parse_args()

    if args.scan_root:
        STATE.config["scan_root"] = args.scan_root
    preferred = args.port or int(STATE.config.get("hub_port") or 7777)
    port = find_free_port(preferred)
    # Only persist the port - paths stay in their original ~/ form so the
    # config file remains portable.
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        on_disk["hub_port"] = port
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(on_disk, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass

    server = ThreadedHTTPServer((args.host, port), HubHandler)

    url = f"http://localhost:{port}/"
    print("=" * 64)
    print(f"  This Is Ultishayaan")
    print(f"  Open:      {url}")
    print(f"  Scan root: {STATE.config.get('scan_root')}")
    print(f"  Logs:      {LOGS_DIR}")
    print(f"  Press Ctrl+C to stop")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hub] shutting down...")
        for runner in list(STATE.runners.values()):
            try:
                runner.stop(timeout=2)
            except Exception:
                pass
        server.shutdown()


if __name__ == "__main__":
    main()
