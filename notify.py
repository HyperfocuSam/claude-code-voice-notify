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
