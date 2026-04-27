#!/usr/bin/env python3
"""run.py — Local relay between the Aurora UI and ai_proxy.py / Claude Code.

Start once:
    python run.py

Endpoints:
  POST /run            — send a prompt to a model
  POST /claude         — run Claude Code
  GET  /ping           — health check
  GET  /memory         — list all memories
  POST /memory/add     — { key, value }
  POST /memory/delete  — { key }
  POST /memory/clear   — wipe all memories
  GET  /storage        — list stored files
  POST /storage/load   — { filename }
  DELETE /storage      — { filename }
  POST /upgrade        — { target, prompt, model } → self-upgrade
  GET  /upgrade/status — last upgrade log
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, subprocess, sys, os, io, tempfile, shutil, datetime, re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROXY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_proxy.py")
RUN   = os.path.abspath(__file__)
PORT  = 8765

PROXY_MODELS = {
    "gemini", "chatgpt", "perplexity", "perplexity_connectors",
    "lechat", "chatai", "arena", "arena_battle", "battle3", "random",
    "pollinations", "flux", "pixelbin", "arena_direct",
}

_upgrade_log = []

def _log_upgrade(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _upgrade_log.append(entry)
    if len(_upgrade_log) > 100:
        _upgrade_log.pop(0)
    print(f"[UPGRADE] {msg}", flush=True)

sys.path.insert(0, os.path.dirname(PROXY))
try:
    import ai_proxy as _proxy
    _PROXY_IMPORTED = True
except Exception as e:
    _PROXY_IMPORTED = False
    print(f"[warn] ai_proxy import failed: {e}", flush=True)


def _reload_proxy():
    global _proxy, _PROXY_IMPORTED
    try:
        import importlib
        if "ai_proxy" in sys.modules:
            importlib.reload(sys.modules["ai_proxy"])
            _proxy = sys.modules["ai_proxy"]
        else:
            import ai_proxy as _proxy
        _PROXY_IMPORTED = True
        return True
    except Exception as e:
        _PROXY_IMPORTED = False
        _log_upgrade(f"Reload failed: {e}")
        return False


def _do_upgrade(target, prompt, model="gemini"):
    """Core upgrade logic. Returns dict {ok, results, log}."""
    targets = ["ai_proxy", "run"] if target == "both" else [target]
    results = {}

    for t in targets:
        file_path = PROXY if t == "ai_proxy" else RUN
        try:
            with open(file_path, encoding="utf-8") as f:
                current_code = f.read()
        except Exception as e:
            results[t] = {"ok": False, "error": f"Could not read file: {e}"}
            continue

        upgrade_prompt = (
            f"You are upgrading a Python file for the Aurora AI chat relay system.\n\n"
            f"CURRENT FILE ({os.path.basename(file_path)}):\n"
            f"```python\n{current_code[:38000]}\n```\n\n"
            f"UPGRADE REQUEST:\n{prompt}\n\n"
            f"STRICT OUTPUT RULES:\n"
            f"1. Output ONLY the complete upgraded Python file — no markdown, no explanation.\n"
            f"2. Do NOT wrap in code fences.\n"
            f"3. Preserve ALL existing functionality unless told otherwise.\n"
            f"4. The file must run as-is with python {os.path.basename(file_path)}.\n"
            f"5. Add # Upgraded: {datetime.datetime.now().strftime('%Y-%m-%d')} near top.\n"
            f"\nOutput the complete Python file now:"
        )

        _log_upgrade(f"Upgrading {t} (len={len(current_code)}) via {model}")
        new_code = None

        # Try Claude Code first
        claude_bin = shutil.which("claude")
        if claude_bin:
            try:
                r = subprocess.run(
                    [claude_bin, "-p", upgrade_prompt],
                    capture_output=True, text=True, timeout=180,
                    encoding="utf-8", errors="replace",
                )
                cand = r.stdout.strip()
                if cand and len(cand) > 300:
                    new_code = cand
                    _log_upgrade(f"Claude Code returned {len(new_code)} chars")
                else:
                    _log_upgrade("Claude Code output too short, trying fallback")
            except Exception as e:
                _log_upgrade(f"Claude Code failed: {e}")

        # Fallback: use ai_proxy with chosen model
        if not new_code:
            try:
                r = subprocess.run(
                    [sys.executable, PROXY, upgrade_prompt, model],
                    capture_output=True, text=True, timeout=300,
                    encoding="utf-8", errors="replace",
                )
                cand = r.stdout.strip()
                if cand.startswith("__HTML__:"):
                    cand = cand[9:]
                m = re.search(r"```(?:python)?\s*([\s\S]+?)```", cand)
                if m:
                    cand = m.group(1).strip()
                if cand and len(cand) > 300:
                    new_code = cand
                    _log_upgrade(f"{model} returned {len(new_code)} chars")
                else:
                    _log_upgrade(f"{model} output too short: {len(cand)}")
            except Exception as e:
                _log_upgrade(f"Model upgrade error: {e}")
                results[t] = {"ok": False, "error": str(e)}
                continue

        if not new_code:
            results[t] = {"ok": False, "error": "All upgrade methods returned insufficient output"}
            continue

        # Validate Python syntax
        try:
            compile(new_code, file_path, "exec")
        except SyntaxError as e:
            _log_upgrade(f"Syntax error: {e}")
            results[t] = {"ok": False, "error": f"Syntax error: {e}", "preview": new_code[:400]}
            continue

        # Backup
        backup = file_path + f".bak.{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copy2(file_path, backup)
            _log_upgrade(f"Backed up to {os.path.basename(backup)}")
        except Exception as e:
            _log_upgrade(f"Backup warning: {e}")
            backup = None

        # Write
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_code)
            _log_upgrade(f"Wrote {t}: {len(new_code)} bytes")
        except Exception as e:
            _log_upgrade(f"Write failed: {e}")
            results[t] = {"ok": False, "error": f"Write failed: {e}"}
            continue

        if t == "ai_proxy":
            ok = _reload_proxy()
            _log_upgrade(f"Reload: {'ok' if ok else 'failed'}")

        results[t] = {
            "ok": True,
            "bytes": len(new_code),
            "backup": backup,
            "preview": new_code[:200] + "…",
        }

    all_ok = bool(results) and all(v.get("ok") for v in results.values())
    return {"ok": all_ok, "results": results, "log": _upgrade_log[-20:]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/ping":
            self._respond({"status": "ok", "version": "2.1"})
        elif self.path == "/memory":
            if _PROXY_IMPORTED:
                self._respond({"memories": _proxy.load_memory()})
            else:
                self._respond({"memories": []})
        elif self.path == "/storage":
            if _PROXY_IMPORTED:
                self._respond({"files": _proxy.list_stored_files()})
            else:
                self._respond({"files": []})
        elif self.path == "/upgrade/status":
            self._respond({"log": _upgrade_log[-50:]})
        else:
            self.send_response(404); self.end_headers()

    def do_DELETE(self):
        if self.path == "/storage":
            body = self._read_body()
            filename = body.get("filename", "")
            if _PROXY_IMPORTED and filename:
                path = _proxy.STORAGE_DIR / filename
                try:
                    if path.exists(): path.unlink()
                    self._respond({"ok": True})
                except Exception as e:
                    self._respond({"ok": False, "error": str(e)})
            else:
                self._respond({"ok": False, "error": "filename required"})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        body = self._read_body()

        if self.path == "/run":
            prompt      = body.get("prompt", "")
            model       = body.get("model", "gemini").lower()
            source      = body.get("source", "native").lower()
            arena_model = body.get("arena_model", "")
            files       = body.get("files", []) or []
            files_tmp   = None
            env         = os.environ.copy()

            if files:
                fd, files_tmp = tempfile.mkstemp(prefix="aurora_files_", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(files, f)
                env["AI_PROXY_FILES"] = files_tmp

            if source == "arena":
                model = "arena_direct"
            if model not in PROXY_MODELS:
                model = "gemini"

            cmd = [sys.executable, PROXY, prompt, model]
            if arena_model:
                cmd.append(arena_model)

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=300, encoding="utf-8", errors="replace", env=env,
                )
                output = result.stdout.strip() or result.stderr.strip() or "(no output)"
            except subprocess.TimeoutExpired:
                output = "Request timed out after 300 seconds."
            except Exception as e:
                output = f"Error: {e}"
            finally:
                if files_tmp and os.path.exists(files_tmp):
                    try: os.remove(files_tmp)
                    except: pass

            self._respond({"output": output})

        elif self.path == "/claude":
            prompt = body.get("prompt", "")
            binary = body.get("binary", "claude")
            cwd    = body.get("cwd", ".") or "."
            cmd    = [binary, "-p", prompt]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=240, cwd=cwd if os.path.isdir(cwd) else None,
                    encoding="utf-8", errors="replace",
                )
                output = result.stdout.strip() or result.stderr.strip() or "(no output)"
                self._respond({"output": output, "cwd": cwd})
            except subprocess.TimeoutExpired:
                self._respond({"output": "Claude Code timed out."})
            except FileNotFoundError:
                self._respond({"output": f"'{binary}' not found. Install Claude Code first."})
            except Exception as e:
                self._respond({"output": f"Error: {e}"})

        elif self.path == "/upgrade":
            target = body.get("target", "ai_proxy")
            prompt = body.get("prompt", "").strip()
            model  = body.get("model", "gemini")
            if not prompt:
                self._respond({"ok": False, "error": "prompt required"})
                return
            result = _do_upgrade(target, prompt, model)
            self._respond(result)

        elif self.path == "/memory/add":
            key, value = body.get("key","").strip(), body.get("value","").strip()
            if not key or not value:
                self._respond({"ok": False, "error": "key and value required"}); return
            if _PROXY_IMPORTED:
                _proxy.add_memory(key, value)
                self._respond({"ok": True, "memories": _proxy.load_memory()})
            else:
                self._respond({"ok": False, "error": "proxy not imported"})

        elif self.path == "/memory/delete":
            key = body.get("key","").strip()
            if not key:
                self._respond({"ok": False, "error": "key required"}); return
            if _PROXY_IMPORTED:
                _proxy.delete_memory(key)
                self._respond({"ok": True, "memories": _proxy.load_memory()})
            else:
                self._respond({"ok": False, "error": "proxy not imported"})

        elif self.path == "/memory/clear":
            if _PROXY_IMPORTED:
                _proxy.save_memory([])
                self._respond({"ok": True})
            else:
                self._respond({"ok": False, "error": "proxy not imported"})

        elif self.path == "/storage/load":
            filename = body.get("filename", "")
            if _PROXY_IMPORTED and filename:
                data_url = _proxy.load_stored_file_as_data_url(filename)
                if data_url:
                    import mimetypes
                    typ = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    self._respond({"ok": True, "dataUrl": data_url, "name": filename, "type": typ})
                else:
                    self._respond({"ok": False, "error": "file not found"})
            else:
                self._respond({"ok": False, "error": "filename required"})

        else:
            self.send_response(404); self.end_headers()

    def _respond(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    print(f"[OK] Aurora relay v2.0 — http://localhost:{PORT}")
    print("  Chat    : POST /run  POST /claude  GET /ping")
    print("  Memory  : GET /memory  POST /memory/add|delete|clear")
    print("  Storage : GET /storage  POST /storage/load  DELETE /storage")
    print("  Upgrade : POST /upgrade  GET /upgrade/status")
    print(f"  Models  : {', '.join(sorted(PROXY_MODELS))}")
    HTTPServer(("localhost", PORT), Handler).serve_forever()
