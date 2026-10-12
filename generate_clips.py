#!/usr/bin/env python3
"""Pre-generate the spoken clips for notify.py, once, with the TTS provider of your choice.

Providers (pick with --provider or "provider" in config.json):
  say         macOS built-in voices. Free, no key.           --voice "Samantha"
  elevenlabs  ELEVENLABS_API_KEY + a voice id.               --voice <voice_id>
  openai      OPENAI_API_KEY.                                --voice alloy

Lines come from projects.json and the templates in config.json. Existing clips
are kept unless --force, so re-running after adding a project costs only the new lines.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("VOICE_NOTIFY_HOME", Path.home() / ".config/claude-voice-notify")).expanduser()
DEFAULTS = {
    "provider": "say",
    "voice": None,
    "fallback_name": "Claude",
    "done_template": "{name} is done.",
    "needs_template": "{name} needs you.",
    "elevenlabs_model": "eleven_multilingual_v2",
    "openai_model": "gpt-4o-mini-tts",
}


def load_env_file(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).expanduser().read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip().removeprefix("export "), v.strip().strip('"').strip("'"))


def post(url: str, headers: dict, body: dict) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def need(var: str) -> str:
    if not os.environ.get(var):
        sys.exit(f"{var} is not set (export it or pass --env-file)")
    return os.environ[var]


def synth(text: str, out_base: Path, cfg: dict) -> Path:
    provider, voice = cfg["provider"], cfg["voice"]
    if provider == "say":
        out = out_base.with_suffix(".aiff")
        subprocess.run(["say", *(["-v", voice] if voice else []), "-o", str(out), text], check=True)
        return out
    out = out_base.with_suffix(".mp3")
    if provider == "elevenlabs":
        if not voice:
            sys.exit("elevenlabs needs a voice id: --voice <id> or \"voice\" in config.json")
        audio = post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
            {"xi-api-key": need("ELEVENLABS_API_KEY")},
            {"text": text, "model_id": cfg["elevenlabs_model"]},
        )
    elif provider == "openai":
        audio = post(
            "https://api.openai.com/v1/audio/speech",
            {"Authorization": f"Bearer {need('OPENAI_API_KEY')}"},
            {"model": cfg["openai_model"], "voice": voice or "alloy", "input": text, "response_format": "mp3"},
        )
    else:
        sys.exit(f"unknown provider: {provider}")
    out.write_bytes(audio)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["say", "elevenlabs", "openai"])
    ap.add_argument("--voice")
    ap.add_argument("--env-file", help="read API keys from a .env file")
    ap.add_argument("--force", action="store_true", help="regenerate clips that already exist")
    args = ap.parse_args()

    load_env_file(args.env_file)
    try:
        cfg = {**DEFAULTS, **json.loads((HOME / "config.json").read_text())}
    except (OSError, ValueError):
        cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in {"provider": args.provider, "voice": args.voice}.items() if v})

    try:
        projects = {k: v for k, v in json.loads((HOME / "projects.json").read_text()).items() if not k.startswith("_")}
    except (OSError, ValueError):
        projects = {}
    names = {"_default": cfg["fallback_name"], **{k: v.get("spoken", k) for k, v in projects.items()}}

    clips = HOME / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    for key, name in names.items():
        for state in ("done", "needs"):
            base = clips / f"{key}__{state}"
            if not args.force and any(clips.glob(f"{base.name}.*")):
                continue
            for old in clips.glob(f"{base.name}.*"):
                old.unlink()
            text = cfg[f"{state}_template"].format(name=name)
            print(f"{synth(text, base, cfg).name}: {text}")


if __name__ == "__main__":
    main()
