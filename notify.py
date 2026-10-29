#!/usr/bin/env python3
"""Claude Code hook: play a jingle, then a short spoken line naming the project.

  Stop          -> "<Project> is done."   ("<Project> needs you." if the reply ends with a question)
  Notification  -> "<Project> needs you." (wire it to the permission_prompt matcher)

The hook never blocks Claude: it forks a detached player and exits 0 at once.
The player waits a few seconds and stays silent if Claude kept working (another
Stop hook blocked, or a background agent woke the session up).

Config lives in $VOICE_NOTIFY_HOME (default ~/.config/claude-voice-notify):
  config.json    optional settings (see examples/config.json)
  projects.json  project key -> keywords + spoken name
  clips/         pre-generated audio, built by generate_clips.py
Mute: create $VOICE_NOTIFY_HOME/muted
Hold (one voice at a time): while the file named by config "hold_file" is live (JSON {"pid", "at"};
ignored when older than "hold_max_age" seconds or when that pid is gone), nothing is played. The event
is POSTed to config "hold_event_url" instead (JSON: project, state, text), e.g. a voice app that is in a call.
Control app (optional): config "control_app" names a menu-bar app that controls these alerts (mute etc.).
If it is not running it is relaunched before anything plays. If it reports, through "control_state_file"
(JSON {"pid", "at", "visible"}), that its icon is not on screen, nothing is played: a silent macOS banner
shows the line instead, so you never hear an alert you have no visible way to stop.
Dry run: VOICE_NOTIFY_DRY_RUN=1 prints what would play instead of playing it.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.environ.get("VOICE_NOTIFY_HOME", Path.home() / ".config/claude-voice-notify")).expanduser()
DEFAULTS = {
    "jingle": "/System/Library/Sounds/Glass.aiff",
    "settle_seconds": 4,
    "scan_entries": 40,
    "hold_file": "",
    "hold_max_age": 7200,
    "hold_event_url": "",
    "control_app": "",
    "control_state_file": "",
}
FALLBACK_KEY = "_default"
LOCK = Path(tempfile.gettempdir()) / "claude_voice_notify.lock"


def load_json(name: str) -> dict:
    try:
        return json.loads((HOME / name).read_text())
    except (OSError, ValueError):
        return {}


def config() -> dict:
    return {**DEFAULTS, **load_json("config.json")}


def projects() -> dict:
    return {k: v for k, v in load_json("projects.json").items() if not k.startswith("_")}


def entry_text(entry: dict) -> str:
    msg = entry.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def read_entries(path: str, start: int = 0) -> list[dict]:
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(start)
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def pick_project(entries: list[dict], scan: int) -> str:
    """Score keyword hits over the recent conversation; later turns weigh more."""
    turns = [e for e in entries if e.get("type") in ("user", "assistant")][-scan:]
    scores: dict[str, float] = {}
    for i, e in enumerate(turns):
        text = entry_text(e).lower()
        weight = 1 + i / max(len(turns), 1)
        for key, p in projects().items():
            hits = sum(len(re.findall(re.escape(kw.lower()), text)) for kw in p.get("keywords", []))
            if hits:
                scores[key] = scores.get(key, 0) + hits * weight
    return max(scores, key=scores.get) if scores else FALLBACK_KEY


def last_assistant_text(payload: dict, entries: list[dict]) -> str:
    if payload.get("last_assistant_message"):
        return str(payload["last_assistant_message"])
    for e in reversed(entries):
        if e.get("type") == "assistant" and entry_text(e).strip():
            return entry_text(e)
    return ""


def player_cmd(path: Path) -> list[str] | None:
    for cmd in (["afplay"], ["paplay"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["mpg123", "-q"], ["aplay", "-q"]):
        if shutil.which(cmd[0]):
            return [*cmd, str(path)]
    return None


def find_clip(key: str, state: str) -> Path | None:
    for k in (key, FALLBACK_KEY):
        for clip in sorted((HOME / "clips").glob(f"{k}__{state}.*")):
            return clip
    return None


def play(key: str, state: str, cfg: dict) -> None:
    sounds = [Path(cfg["jingle"])] if cfg.get("jingle") else []
    clip = find_clip(key, state)
    if clip:
        sounds.append(clip)
    with open(LOCK, "w") as lock:  # one session speaks at a time
        fcntl.flock(lock, fcntl.LOCK_EX)
        for s in sounds:
            cmd = player_cmd(s) if s.exists() else None
            if cmd:
                subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hold_live(cfg: dict) -> bool:
    """True while another voice (e.g. a live voice call) owns the speakers."""
    f = Path(os.path.expanduser(cfg.get("hold_file") or ""))
    if not cfg.get("hold_file") or not f.exists():
        return False
    try:
        h = json.loads(f.read_text())
        if time.time() - float(h.get("at", 0)) > float(cfg.get("hold_max_age", 7200)):
            return False
        os.kill(int(h["pid"]), 0)          # raises if the owner process is gone
        return True
    except (ValueError, KeyError, TypeError, OSError):
        return False


def post_event(cfg: dict, key: str, state: str) -> None:
    url = cfg.get("hold_event_url")
    if not url:
        return
    name = key.strip("_").capitalize() if key != FALLBACK_KEY else "A session"
    body = {"project": name, "state": state, "text": f"{name}: {'needs you' if state == 'needs' else 'back to you'}",
            "source": "claude-code-voice-notify"}
    try:
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def control_visible(cfg: dict) -> bool:
    """Make sure the control app runs; True unless it reports its icon is hidden."""
    app = os.path.expanduser(cfg.get("control_app") or "")
    if not app or sys.platform != "darwin":
        return True
    exe = Path(app).stem.replace(" ", "")
    running = subprocess.run(["pgrep", "-x", exe], capture_output=True).returncode == 0 or \
        subprocess.run(["pgrep", "-f", f"{app}/Contents/MacOS/"], capture_output=True).returncode == 0
    if not running:
        subprocess.run(["open", "-g", app], check=False)
        time.sleep(3)   # let it start and report where its icon is
    f = Path(os.path.expanduser(cfg.get("control_state_file") or ""))
    if not cfg.get("control_state_file") or not f.exists():
        return True
    try:
        st = json.loads(f.read_text())
        if time.time() - float(st.get("at", 0)) > 30:
            return True    # stale report: don't silence on old news
        return bool(st.get("visible", True))
    except (ValueError, OSError, TypeError):
        return True


def banner(key: str, state: str, cfg: dict) -> None:
    name = key.strip("_").capitalize() if key != FALLBACK_KEY else "A session"
    line = f"{name}: {'needs you' if state == 'needs' else 'done'}"
    msg = "Alert icon hidden in the menu bar, so this one is silent."
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title {json.dumps(line)}"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def player(payload: dict) -> None:
    cfg = config()
    event = payload.get("hook_event_name", "")
    transcript = payload.get("transcript_path", "")
    size = os.path.getsize(transcript) if transcript and os.path.exists(transcript) else 0
    time.sleep(cfg["settle_seconds"])
    if event == "Stop" and any(e.get("type") == "assistant" for e in read_entries(transcript, size)):
        return  # Claude carried on; this was not the real end of the turn
    entries = read_entries(transcript) if transcript else []
    key = pick_project(entries, cfg["scan_entries"])
    if event == "Stop":
        asks = last_assistant_text(payload, entries).rstrip(" *`\n").endswith("?")
        state = "needs" if asks else "done"
    else:
        state = "needs"
    if hold_live(cfg):
        post_event(cfg, key, state)   # another voice is talking: hand the event over silently
        if os.environ.get("VOICE_NOTIFY_DRY_RUN"):
            print(f"held: {key} {state} -> {cfg.get('hold_event_url')}")
        return
    if not control_visible(cfg):
        if os.environ.get("VOICE_NOTIFY_DRY_RUN"):
            print(f"banner (control icon hidden): {key} {state}")
            return
        banner(key, state, cfg)
        return
    if os.environ.get("VOICE_NOTIFY_DRY_RUN"):
        print(f"would play: {key} {state} jingle={cfg.get('jingle')} clip={find_clip(key, state)}")
        return
    play(key, state, cfg)


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--player":
        player(json.loads(sys.argv[2]))
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if (HOME / "muted").exists() or payload.get("agent_id"):
        return 0  # muted, or a subagent finishing
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--player", json.dumps(payload)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, "VOICE_NOTIFY_HOME": str(HOME)},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
