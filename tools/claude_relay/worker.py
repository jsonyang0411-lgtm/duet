#!/usr/bin/env python3
"""Claude relay worker (runs on the Mac): jobs from the WeChat bot, done by Claude Code.

The Windows bot (CLAUDE_RELAY=on) queues "cc ..." requests.  This worker asks
for them over HTTP (long poll, so the Mac needs no open port), runs

    claude -p --output-format json --permission-mode auto --permission-prompts none
           [--resume <session>] --append-system-prompt <how to answer>

in the project folder, sends a heartbeat every 30 s (the answer says when the
user cancelled), and posts the result back.  A result is written to the
outbox first and sent until the bot has it, so a Windows restart loses nothing.

Standard library only (macOS /usr/bin/python3 is enough).  Settings:
~/.claude-relay/config.json (see config.example.json); the token can live in
its own file (token_file); RELAY_URL and RELAY_TOKEN in the environment win.  Setup: README.md next to this file.

    python3 worker.py --check     reach the bot and find claude, then exit
    python3 worker.py --once      do one job, then exit
    python3 worker.py             run until stopped
    --stay-awake                  keep the Mac awake while it is on power (caffeinate -s)

Only one worker runs at a time (a lock in ~/.claude-relay): a second one exits.
To keep it running, start-worker.command opens it in Terminal at login.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME_DIR = Path(os.environ.get("CLAUDE_RELAY_HOME", Path.home() / ".claude-relay"))
POLL_WAIT_SEC = 25
HEARTBEAT_SEC = 30
STOP_GRACE_SEC = 30          # after SIGINT, wait this long before killing claude
MAX_BACKOFF_SEC = 60

DEFAULTS = {
    "relay_url": "",
    "token": "",
    "token_file": "",          # a file holding only the token (kept out of config.json)
    "projects": {},            # alias -> folder, for "cc @alias ..."
    "default_project": "",     # alias or folder for tasks without @alias (empty = home)
    "claude_bin": "",          # empty = PATH, the usual install places, then VS Code's copy
    "permission_mode": "auto",
    "max_budget_usd": 0,       # 0 = no cap (--max-budget-usd)
    "timeout_min": 60,
    "model": "",
    "extra_args": [],
}

SYSTEM_NOTE = (
    "这条任务来自微信：Windows 上的微信机器人把消息转给了你（webot 的 Claude 中转）。"
    "发消息的人不在电脑前，没法回答你的问题，也没法批准权限；需要人工批准的操作会被直接拒绝，"
    "遇到时换一种不需要批准的办法，或者在结果里说清楚需要对方做什么。信息不够时按最合理的理解去做，"
    "并在结果里写明你的假设。不要做发送消息、推送代码、删除大量文件这类对外或难以撤销的事，"
    "除非任务里明确要求。你最后的回复会原样发到微信：用中文，先一句话说结论，再列关键改动或发现"
    "（文件路径、命令结果），整体不超过 300 字，不要用 Markdown 表格和代码块。"
)

log = logging.getLogger("claude_relay.worker")


# ── settings ─────────────────────────────────────────────────────────────

def load_settings(path: Path) -> dict:
    settings = dict(DEFAULTS)
    if path.exists():
        settings.update(json.loads(path.read_text(encoding="utf-8")))
    if settings.get("token_file") and not os.environ.get("RELAY_TOKEN"):
        token_path = Path(os.path.expanduser(str(settings["token_file"])))
        try:
            settings["token"] = token_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SystemExit(f"cannot read token_file {token_path}: {e}")
    for env, key in (("RELAY_URL", "relay_url"), ("RELAY_TOKEN", "token")):
        if os.environ.get(env):
            settings[key] = os.environ[env]
    settings["relay_url"] = str(settings["relay_url"]).rstrip("/")
    if not settings["relay_url"] or not settings["token"]:
        raise SystemExit(f"relay_url and token are needed (in {path} or RELAY_URL / RELAY_TOKEN)")
    return settings


def read_token(path: Path) -> str | None:
    """The token config.json (or RELAY_TOKEN) holds now; None if it cannot be read."""
    try:
        return load_settings(path)["token"]
    except (SystemExit, Exception):
        return None


def find_claude(configured: str = "") -> str:
    """The claude executable, looked up for every job (VS Code updates move its copy)."""
    candidates = [configured] if configured else []
    candidates += [
        shutil.which("claude") or "",
        str(Path.home() / ".local/bin/claude"),
        str(Path.home() / ".claude/local/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.expanduser(c)) and os.access(os.path.expanduser(c), os.X_OK):
            return os.path.expanduser(c)
    bundled = glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"))

    def version(path: str) -> tuple:
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", path)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    bundled = [p for p in bundled if os.access(p, os.X_OK)]
    if bundled:
        return max(bundled, key=version)
    raise FileNotFoundError("claude not found: set claude_bin in config.json")


def project_dir(settings: dict, alias: str) -> tuple[Path | None, str]:
    """The folder for a job, or (None, why not)."""
    projects = {str(k): str(v) for k, v in (settings.get("projects") or {}).items()}
    if alias:
        if alias not in projects:
            known = "、".join(sorted(projects)) or "（还没有配置项目）"
            return None, f"Mac 上没有叫 {alias} 的项目。可用的有：{known}"
        folder = projects[alias]
    else:
        default = str(settings.get("default_project") or "")
        folder = projects.get(default, default) or str(Path.home())
    path = Path(os.path.expanduser(folder))
    if not path.is_dir():
        return None, f"Mac 上的项目目录不存在：{path}"
    return path, ""


# ── talking to the bot ───────────────────────────────────────────────────

class RelayClient:
    """HTTP to the bot.  Proxies are ignored: Clash on this Mac must not carry LAN traffic.

    ``reload_token`` (optional) reads the token again when the bot refuses the
    current one: put the new token in config.json, change it on Windows when
    convenient, and the running worker follows without a restart.
    """

    def __init__(self, base_url: str, token: str, reload_token=None):
        self.base_url = base_url
        self.token = token
        self.reload_token = reload_token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 15):
        try:
            return self._send(method, path, body, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401 or self.reload_token is None:
                raise
            token = self.reload_token()
            if not token or token == self.token:
                raise
            log.info("the bot refused the token; config.json has another one, using it")
            self.token = token
            return self._send(method, path, body, timeout)

    def _send(self, method: str, path: str, body: dict | None, timeout: float):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)

    def health(self) -> bool:
        status, data = self._call("GET", "/relay/health")
        return status == 200 and bool(data and data.get("ok"))

    def next_job(self, wait: int = POLL_WAIT_SEC) -> dict | None:
        status, data = self._call("GET", f"/relay/next?wait={wait}", timeout=wait + 15)
        return data if status == 200 else None

    def heartbeat(self, job_id: int) -> dict | None:
        try:
            _, data = self._call("POST", f"/relay/jobs/{job_id}/heartbeat", {})
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def send_result(self, job_id: int, result: dict) -> bool:
        """True when the bot has it (or does not know the job: then there is nobody to tell)."""
        try:
            _, data = self._call("POST", f"/relay/jobs/{job_id}/result", result)
            log.info("#%s result delivered (accepted=%s)", job_id, data and data.get("accepted"))
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log.warning("#%s unknown to the bot; result dropped", job_id)
                return True
            raise


# ── running one job ──────────────────────────────────────────────────────

class Stopper:
    """Ctrl+C / launchd stop: finish the current job's bookkeeping, take no new one."""

    def __init__(self):
        self.event = threading.Event()

    def __call__(self, signum, frame):
        log.info("stop requested (signal %s)", signum)
        self.event.set()


def claude_command(settings: dict, session_id: str | None) -> list[str]:
    cmd = [
        find_claude(settings.get("claude_bin", "")), "-p", "--output-format", "json",
        "--permission-mode", str(settings.get("permission_mode") or "auto"),
        "--permission-prompts", "none",
        "--append-system-prompt", SYSTEM_NOTE,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    if float(settings.get("max_budget_usd") or 0) > 0:
        cmd += ["--max-budget-usd", str(settings["max_budget_usd"])]
    if settings.get("model"):
        cmd += ["--model", str(settings["model"])]
    cmd += [str(a) for a in settings.get("extra_args") or []]
    return cmd


def run_claude(settings: dict, job: dict, cwd: Path, client: RelayClient, stopper: Stopper,
               session_id: str | None) -> dict:
    """Run claude once; heartbeat meanwhile.  Returns stdout/stderr/returncode and why it stopped."""
    cmd = claude_command(settings, session_id)
    # The task goes in on stdin: a task starting with "-" must not be read as an option.
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", start_new_session=True,
    )
    out: dict = {}

    def communicate():
        out["stdout"], out["stderr"] = proc.communicate(job["text"])

    reader = threading.Thread(target=communicate, daemon=True)
    reader.start()
    started = time.monotonic()
    deadline = started + float(settings.get("timeout_min") or 60) * 60
    stopped_because, signalled_at = "", None
    while reader.is_alive():
        reader.join(HEARTBEAT_SEC)
        if not reader.is_alive():
            break
        try:
            answer = client.heartbeat(job["id"])
        except Exception as e:                           # the bot may be restarting
            log.warning("#%s heartbeat failed: %s", job["id"], e)
            answer = None
        reason = ""
        if answer and answer.get("cancel"):
            reason = "cancelled"
        elif time.monotonic() > deadline:
            reason = "timeout"
        elif stopper.event.is_set():
            reason = "worker_stopped"
        if reason and signalled_at is None:
            log.info("#%s stopping claude (%s)", job["id"], reason)
            stopped_because, signalled_at = reason, time.monotonic()
            proc.send_signal(signal.SIGINT)              # ends the turn cleanly
        elif signalled_at is not None and time.monotonic() - signalled_at > STOP_GRACE_SEC:
            log.warning("#%s claude did not stop; killing it", job["id"])
            os.killpg(proc.pid, signal.SIGKILL)
    reader.join()
    return {
        "stdout": out.get("stdout", ""), "stderr": out.get("stderr", ""),
        "returncode": proc.returncode, "stopped": stopped_because,
        "duration_s": round(time.monotonic() - started, 1),
    }


def parse_output(stdout: str) -> dict | None:
    """The final JSON object claude prints (None when it printed something else)."""
    text = stdout.strip()
    try:
        data = json.loads(text)
    except ValueError:
        start = text.rfind("\n{")
        try:
            data = json.loads(text[start + 1:]) if start >= 0 else None
        except ValueError:
            data = None
    return data if isinstance(data, dict) else None


def summarize(run: dict, data: dict | None, settings: dict) -> dict:
    """The result for the bot: ok, text, session_id, duration_s, cost_usd, error."""
    result = {"duration_s": run["duration_s"]}
    if run["stopped"] == "cancelled":
        return {**result, "ok": False, "text": "已按你的要求停下。",
                "session_id": (data or {}).get("session_id", "")}
    if run["stopped"] == "timeout":
        return {**result, "ok": False, "session_id": (data or {}).get("session_id", ""),
                "text": f"超过 {settings.get('timeout_min')} 分钟还没做完，已经停下。可以回复「重试」或把任务拆小。"}
    if run["stopped"] == "worker_stopped":
        return {**result, "ok": False, "text": "Mac 上的 worker 被关掉了，任务没做完。"}
    if data is None:
        tail = (run["stderr"] or run["stdout"]).strip()[-600:]
        return {**result, "ok": False, "text": "", "error": f"claude 没有正常输出（退出码 {run['returncode']}）：{tail}"}
    text = str(data.get("result") or "").strip()
    denied = data.get("permission_denials") or []
    if denied:
        tools = sorted({str(d.get("tool_name", "?")) for d in denied if isinstance(d, dict)})
        text += f"\n（有 {len(denied)} 个操作需要人工批准，已被拒绝：{', '.join(tools)}）"
    ok = run["returncode"] == 0 and not data.get("is_error")
    return {
        **result, "ok": ok, "text": text, "session_id": str(data.get("session_id") or ""),
        "cost_usd": data.get("total_cost_usd"),
        **({} if ok else {"error": str(data.get("subtype") or "error")}),
    }


def session_projects() -> dict:
    """Which project each Claude session started in (``~/.claude-relay/sessions.json``)."""
    try:
        data = json.loads((HOME_DIR / "sessions.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_session_project(session_id: str, project: str) -> None:
    if not session_id:
        return
    data = session_projects()
    data[session_id] = project
    if len(data) > 500:                                  # oldest first (insertion order)
        data = dict(list(data.items())[-500:])
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    (HOME_DIR / "sessions.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def do_job(settings: dict, job: dict, client: RelayClient, stopper: Stopper) -> dict:
    session_id = job.get("session_id") or None
    # A follow-up without @project continues where its session started: "改一下那个文件"
    # after "cc @relay ..." must not run in the default project.
    project = job.get("project") or (session_projects().get(session_id, "") if session_id else "")
    log.info("#%s start%s: %s", job["id"], f" @{project}" if project else "", job["text"][:100])
    cwd, problem = project_dir(settings, project)
    if cwd is None:
        return {"ok": False, "text": problem, "duration_s": 0}
    run = run_claude(settings, job, cwd, client, stopper, session_id)
    data = parse_output(run["stdout"])
    if data is None and session_id and "No conversation found" in run["stdout"] + run["stderr"]:
        log.info("#%s session %s is gone; starting a new one", job["id"], session_id)
        run = run_claude(settings, job, cwd, client, stopper, None)
        data = parse_output(run["stdout"])
    result = summarize(run, data, settings)
    remember_session_project(result.get("session_id", ""), project)
    result["log_path"] = str(save_log(job, run, result))
    log.info("#%s finished ok=%s in %ss", job["id"], result["ok"], result["duration_s"])
    return result


def save_log(job: dict, run: dict, result: dict) -> Path:
    folder = HOME_DIR / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-job{job['id']}.md"
    path.write_text(
        f"# #{job['id']} {job.get('project') or ''}\n\n## 任务\n\n{job['text']}\n\n"
        f"## 结果（ok={result['ok']}）\n\n{result.get('text') or result.get('error') or ''}\n\n"
        f"## claude stdout\n\n{run['stdout']}\n\n## claude stderr\n\n{run['stderr']}\n",
        encoding="utf-8",
    )
    return path


# ── delivery that survives restarts ──────────────────────────────────────

def outbox_dir() -> Path:
    folder = HOME_DIR / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def deliver(client: RelayClient, job_id: int, result: dict, stopper: Stopper) -> bool:
    """Write the result to the outbox, then send it until the bot has it."""
    path = outbox_dir() / f"{job_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return flush_one(client, path, stopper)


def flush_one(client: RelayClient, path: Path, stopper: Stopper) -> bool:
    job_id = int(path.stem)
    result = json.loads(path.read_text(encoding="utf-8"))
    backoff = 5
    while True:
        try:
            if client.send_result(job_id, result):
                path.unlink(missing_ok=True)
                return True
        except Exception as e:
            log.warning("#%s result not delivered (%s); retry in %ss", job_id, e, backoff)
        if stopper.event.wait(backoff):
            log.info("#%s result stays in %s for the next start", job_id, path)
            return False
        backoff = min(backoff * 2, MAX_BACKOFF_SEC)


def flush_outbox(client: RelayClient, stopper: Stopper) -> None:
    for path in sorted(outbox_dir().glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        if path.stem.isdigit() and not flush_one(client, path, stopper):
            return


# ── main loop ────────────────────────────────────────────────────────────

def serve(settings: dict, client: RelayClient, stopper: Stopper, once: bool = False) -> None:
    flush_outbox(client, stopper)
    backoff = 5
    while not stopper.event.is_set():
        try:
            job = client.next_job()
            backoff = 5
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log.error("the bot refused the token (401): RELAY_TOKEN must equal CLAUDE_RELAY_TOKEN")
            else:
                log.warning("asking for a job failed: HTTP %s", e.code)
            stopper.event.wait(MAX_BACKOFF_SEC if e.code == 401 else backoff)
            continue
        except Exception as e:                           # bot down, Wi-Fi changed, ...
            log.warning("cannot reach the bot (%s); retry in %ss", e, backoff)
            stopper.event.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)
            continue
        if job is None:
            continue
        try:
            result = do_job(settings, job, client, stopper)
        except Exception as e:
            log.exception("#%s failed in the worker", job.get("id"))
            result = {"ok": False, "text": "", "error": f"Mac 上的 worker 出错：{e}", "duration_s": 0}
        deliver(client, int(job["id"]), result, stopper)
        if once:
            return


def single_instance():
    """The lock that keeps a second worker from starting (two would race for jobs and sessions)."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(HOME_DIR / "worker.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def stay_awake() -> subprocess.Popen | None:
    """caffeinate -s for as long as this process lives (awake on power, normal sleep on battery)."""
    try:
        return subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())])
    except OSError as e:
        log.warning("caffeinate not started: %s", e)
        return None


def setup_logging() -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stderr), logging.FileHandler(HOME_DIR / "worker.log", encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(HOME_DIR / "config.json"))
    parser.add_argument("--check", action="store_true", help="reach the bot and find claude, then exit")
    parser.add_argument("--once", action="store_true", help="do one job, then exit")
    parser.add_argument("--stay-awake", action="store_true", help="keep the Mac awake while on power")
    args = parser.parse_args(argv)
    setup_logging()
    settings = load_settings(Path(args.config))
    client = RelayClient(settings["relay_url"], settings["token"],
                         reload_token=lambda: read_token(Path(args.config)))
    if args.check:
        claude = find_claude(settings.get("claude_bin", ""))
        version = subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"claude: {claude} ({version})")
        print(f"default folder: {project_dir(settings, '')[0] or project_dir(settings, '')[1]}")
        for alias in sorted(settings.get("projects") or {}):
            folder, problem = project_dir(settings, alias)
            print(f"  @{alias}: {folder or problem}")
        try:
            print(f"bot {settings['relay_url']}: {'ok' if client.health() else 'unexpected answer'}")
        except urllib.error.HTTPError as e:
            print(f"bot {settings['relay_url']}: HTTP {e.code}" + (" (token wrong)" if e.code == 401 else ""))
            return 1
        except Exception as e:
            print(f"bot {settings['relay_url']}: unreachable ({e})")
            return 1
        return 0
    lock = single_instance()
    if lock is None:
        log.error("another worker is already running (%s); this one exits", HOME_DIR / "worker.lock")
        return 0
    if args.stay_awake:
        stay_awake()
    stopper = Stopper()
    signal.signal(signal.SIGINT, stopper)
    signal.signal(signal.SIGTERM, stopper)
    signal.signal(signal.SIGHUP, stopper)       # the Terminal window was closed: finish the job's bookkeeping
    log.info("worker started; bot %s", settings["relay_url"])
    serve(settings, client, stopper, once=args.once)
    log.info("worker stopped")
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
