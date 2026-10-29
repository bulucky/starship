# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Cached Open-Meteo weather for Starship; stdout contains only band<TAB>icon.

prompt never waits for HTTP. It schedules a detached worker when necessary.
refresh is an explicit, synchronous diagnostic/setup command; it stays quiet.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
COLORS = ("blue", "green", "yellow", "pink")
# Nerd Fonts Material Design weather glyphs, not multicolor emoji.
ICONS = {
    "sun": "\U000f0599", "moon": "\U000f0594",
    "partly_day": "\U000f0595", "partly_night": "\U000f0f31",
    "cloud": "\U000f0590", "fog": "\U000f0591",
    "rain": "\U000f0597", "pouring": "\U000f0596",
    "snow": "\U000f0598", "sleet": "\U000f067f",
    "storm": "\U000f0593",
}


def number(value, low, high):
    if isinstance(value, bool):
        raise ValueError("boolean is not a coordinate/temperature")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError("number out of range")
    return result


def settings(root=ROOT):
    config = {
        "latitude": None, "longitude": None, "refresh_seconds": 900,
        "max_stale_seconds": 7200, "retry_seconds": 300,
        "temperature_thresholds_c": [10, 25, 32],
    }
    for name in ("weather.json", "weather.local.json"):
        path = root / name
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("weather configuration must be an object")
            config.update(value)
    for key in ("latitude", "longitude"):
        override = os.environ.get("STARSHIP_WEATHER_" + key.upper())
        if override is not None:
            config[key] = override
    if config.get("enabled", True) is False:
        return None
    if config["latitude"] is None or config["longitude"] is None:
        return None
    config["latitude"] = number(config["latitude"], -90, 90)
    config["longitude"] = number(config["longitude"], -180, 180)
    for key in ("refresh_seconds", "max_stale_seconds", "retry_seconds"):
        config[key] = number(config[key], 60, 86400)
    if config["max_stale_seconds"] < config["refresh_seconds"]:
        raise ValueError("max stale time must be >= refresh time")
    thresholds = config["temperature_thresholds_c"]
    if not isinstance(thresholds, list) or len(thresholds) != 3:
        raise ValueError("three ordered temperature thresholds required")
    thresholds = [number(value, -100, 100) for value in thresholds]
    if not thresholds[0] < thresholds[1] < thresholds[2]:
        raise ValueError("temperature thresholds must be increasing")
    config["temperature_thresholds_c"] = thresholds
    return config


def cache_directory():
    override = os.environ.get("STARSHIP_WEATHER_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "starship/weather"


def paths(config):
    location = f'{config["latitude"]:.6f},{config["longitude"]:.6f}'
    key = hashlib.sha256(location.encode("ascii")).hexdigest()[:16]
    base = cache_directory()
    return {name: base / (key + suffix) for name, suffix in (
        ("data", ".json"), ("attempt", ".attempt.json"), ("lock", ".lock"),
    )}


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".weather-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def icon_for(code, is_day):
    if type(code) is not int or type(is_day) is not int or is_day not in (0, 1):
        return None
    if code in (0, 1):
        name = "sun" if is_day else "moon"
    elif code == 2:
        name = "partly_day" if is_day else "partly_night"
    elif code == 3:
        name = "cloud"
    elif code in (45, 48):
        name = "fog"
    elif code in (51, 53, 55, 61, 63, 80, 81):
        name = "rain"
    elif code in (65, 82):
        name = "pouring"
    elif code in (56, 57, 66, 67):
        name = "sleet"
    elif code in (71, 73, 75, 77, 85, 86):
        name = "snow"
    elif code in (95, 96, 97, 99):
        name = "storm"
    else:
        return None
    return ICONS[name]


def validated_current(value):
    if not isinstance(value, dict):
        raise ValueError("missing current conditions")
    if icon_for(value.get("weather_code"), value.get("is_day")) is None:
        raise ValueError("unsupported weather code or day/night flag")
    return {
        "weather_code": value["weather_code"], "is_day": value["is_day"],
        "temperature_2m": number(value.get("temperature_2m"), -100, 100),
    }


def age(record, key, now):
    try:
        timestamp = number(record.get(key), 0, now + 60)
        return max(0, now - timestamp)
    except (ValueError, TypeError):
        return math.inf


def display(config, data, now):
    if age(data, "fetched_at", now) > config["max_stale_seconds"]:
        return ""
    try:
        current = validated_current(data.get("current"))
    except (ValueError, TypeError):
        return ""
    band = sum(current["temperature_2m"] >= limit
               for limit in config["temperature_thresholds_c"])
    return COLORS[band] + "\t" + icon_for(current["weather_code"], current["is_day"])


def acquire_lock(lock, now):
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
        return True
    except FileExistsError:
        try:
            if now - lock.stat().st_mtime > 60:
                lock.rmdir()
                lock.mkdir()
                return True
        except OSError:
            pass
        return False


def release_lock(lock):
    try:
        lock.rmdir()
    except OSError:
        pass


def refresh(config, locations):
    query = urlencode({
        "latitude": config["latitude"], "longitude": config["longitude"],
        "current": "weather_code,is_day,temperature_2m", "timezone": "auto",
        "temperature_unit": "celsius",
    })
    request = Request("https://api.open-meteo.com/v1/forecast?" + query,
                      headers={"User-Agent": "starship-weather/1.0"})
    with urlopen(request, timeout=3) as response:
        # Bound response size; only current data is requested.
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("weather response too large")
    data = json.loads(payload)
    current = validated_current(data.get("current"))
    write_json(locations["data"], {"fetched_at": time.time(), "current": current})


def spawn_worker():
    arguments = [sys.executable, str(Path(__file__).resolve()), "worker"]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": str(ROOT)}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(arguments, **options)


def prompt(config, locations, now):
    data = read_json(locations["data"])
    output = display(config, data, now)
    attempt = read_json(locations["attempt"])
    locked = False
    try:
        if (age(data, "fetched_at", now) >= config["refresh_seconds"]
                and age(attempt, "at", now) >= config["retry_seconds"]):
            locked = acquire_lock(locations["lock"], now)
        if locked:
            write_json(locations["attempt"], {"at": now})
            spawn_worker()
    except Exception:
        # Refresh is best-effort; a readable cache remains useful even when
        # the directory is read-only or a lock/worker cannot be created.
        if locked:
            release_lock(locations["lock"])
    return output


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else "prompt"
    if mode not in ("prompt", "refresh", "worker"):
        return 2
    locations = None
    locked = False
    try:
        config = settings()
        if config is None:
            return 0
        locations = paths(config)
        now = time.time()
        if mode == "prompt":
            output = prompt(config, locations, now)
            if output:
                sys.stdout.reconfigure(encoding="utf-8")
                print(output)
            return 0
        locked = mode == "worker" or acquire_lock(locations["lock"], now)
        if not locked:
            return 0
        write_json(locations["attempt"], {"at": now})
        refresh(config, locations)
        return 0
    except Exception as error:
        # A prompt enhancement must not print errors into the shell.
        if mode == "refresh":
            print(f"Weather refresh failed: {error}", file=sys.stderr)
        return 0 if mode == "prompt" else 1
    finally:
        if locked and locations is not None:
            release_lock(locations["lock"])


if __name__ == "__main__":
    raise SystemExit(main())
