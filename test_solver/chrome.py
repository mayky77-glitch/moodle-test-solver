from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def find_chrome() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise FileNotFoundError("Chrome/Chromium was not found")


def launch_chrome(profile: str | Path, port: int, use_default_profile: bool = False) -> subprocess.Popen:
    chrome = find_chrome()
    command = [
        chrome,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if not use_default_profile:
        profile_path = Path(profile)
        profile_path.mkdir(parents=True, exist_ok=True)
        command.append(f"--user-data-dir={profile_path.resolve()}")
    return subprocess.Popen(command)
