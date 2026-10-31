# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Cached Open-Meteo weather; prompt stdout is #rrggbb<TAB>hex codepoint.

prompt never waits for HTTP. It schedules a detached worker when necessary.
refresh is an explicit, synchronous diagnostic/setup command; it stays quiet.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ICONS = {
    "sun": ["f522"], "moon": ["f4ee", "f0594"],
    "partly_day": ["f0595"], "partly_night": ["f0f31"],
    "cloud": ["f0c2"], "fog_day": ["f0591", "e303"],
    "fog_night": ["f0591", "e346"], "rain_day": ["ef1c", "ef1e"],
    "rain_night": ["ef1c", "ef1b"], "pouring": ["f0596"],
    "snow": ["f2dc", "f0598"], "sleet_day": ["e362"],
    "sleet_night": ["f067f"], "storm": ["f0593"],
}
DEFAULT_APPEARANCE = {
    "timezone": "Asia/Shanghai",
    "rest_icon": "f04b2",
    # Ordered from the start of the active period, crossing midnight once.
    # Between the final stop and the next first stop, hold the final color.
    "color_stops": [
        {"time": "06:00", "color": "#8aadf4"},
        {"time": "12:00", "color": "#eed49f"},
        {"time": "18:00", "color": "#ed8796"},
        {"time": "22:00", "color": "#c6a0f6"},
        {"time": "02:00", "color": "#8087a2"},
    ],
    "temperature": {
        "neutral_c": 22, "cold_c": 10, "hot_c": 32, "max_mix": 0.2,
        "cold_color": "#8aadf4", "hot_color": "#ed8796",
    },
}


def number(value, low, high, name="value"):
    if isinstance(value, bool):
        raise ValueError(f"{name}: expected a number, not boolean")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected a number") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name}: must be between {low} and {high}")
    return result


def merge_settings(base, override, path="configuration"):
    """Merge objects; replace arrays so local icon lists never append silently."""
    if not isinstance(override, dict):
        raise ValueError(f"{path}: expected an object")
    for key, value in override.items():
        if isinstance(base.get(key), dict):
            merge_settings(base[key], value, f"{path}.{key}")
        else:
            base[key] = deepcopy(value)


def minute_of_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f"time: expected HH:MM, got {value!r}")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def rgb(value, name="color"):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"{name}: expected #rrggbb")
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def private_codepoint(code, name):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9a-fA-F]{4,6}", code):
        raise ValueError(f"{name}: expected a hexadecimal Nerd Font codepoint")
    point = int(code, 16)
    if not (0xe000 <= point <= 0xf8ff or 0xf0000 <= point <= 0xffffd
            or 0x100000 <= point <= 0x10fffd):
        raise ValueError(f"{name}: expected a Unicode private-use codepoint")
    return point


def load_settings(root=ROOT):
    """Read and merge JSON values without changing their representation."""
    config = {
        "latitude": None, "longitude": None, "refresh_seconds": 900,
        "max_stale_seconds": 7200, "retry_seconds": 300,
        "icons": deepcopy(DEFAULT_ICONS), "appearance": deepcopy(DEFAULT_APPEARANCE),
    }
    for name in ("weather.json", "weather.local.json"):
        path = root / name
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            merge_settings(config, value, name)
    for key in ("latitude", "longitude"):
        override = os.environ.get("STARSHIP_WEATHER_" + key.upper())
        if override is not None:
            config[key] = override
    return config


def settings(root=ROOT, require_location=True):
    raw = load_settings(root)
    config = {key: value for key, value in raw.items() if key not in ("icons", "appearance")}
    if require_location and config.get("enabled", True) is False:
        return None
    if require_location and (config["latitude"] is None or config["longitude"] is None):
        return None
    if require_location:
        config["latitude"] = number(config["latitude"], -90, 90, "latitude")
        config["longitude"] = number(config["longitude"], -180, 180, "longitude")
    for key in ("refresh_seconds", "max_stale_seconds", "retry_seconds"):
        config[key] = number(config[key], 60, 86400, key)
    if config["max_stale_seconds"] < config["refresh_seconds"]:
        raise ValueError("max stale time must be >= refresh time")
    config["icons"] = parse_icons(raw["icons"])
    config["appearance"] = parse_appearance(raw["appearance"])
    return config


def parse_icons(raw):
    icons = {}
    for kind, codes in raw.items():
        if kind not in DEFAULT_ICONS:
            raise ValueError(f"icons.{kind}: unknown weather category")
        if not isinstance(codes, list) or not codes:
            raise ValueError(f"icons.{kind}: expected a nonempty array")
        icons[kind] = tuple(dict.fromkeys(
            private_codepoint(code, f"icons.{kind}[{index}]")
            for index, code in enumerate(codes)))
    return icons


def parse_appearance(appearance):
    """Compile display rules into a separate, consistently typed structure."""
    for key in appearance:
        if key not in DEFAULT_APPEARANCE:
            raise ValueError(f"appearance.{key}: unknown setting")
    zone = appearance["timezone"]
    if not isinstance(zone, str) or not zone:
        raise ValueError("appearance.timezone: expected an IANA timezone name")
    try:
        parsed_zone = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        if zone != "Asia/Shanghai":
            raise ValueError(f"appearance.timezone: timezone data unavailable for {zone}") from None
        # Modern Shanghai time; Windows Python need not install tzdata.
        parsed_zone = timezone(timedelta(hours=8), name=zone)
    stops = appearance["color_stops"]
    if not isinstance(stops, list) or not 2 <= len(stops) <= 24:
        raise ValueError("appearance.color_stops: expected 2 to 24 ordered stops")
    compiled = []
    for index, stop in enumerate(stops):
        name = f"appearance.color_stops[{index}]"
        if not isinstance(stop, dict) or set(stop) != {"time", "color"}:
            raise ValueError(f"{name}: expected time and color")
        try:
            minute = minute_of_day(stop["time"])
        except ValueError as error:
            raise ValueError(f"{name}.{error}") from None
        if compiled:
            minute = compiled[0][0] + (minute - compiled[0][0]) % 1440
            if minute <= compiled[-1][0]:
                raise ValueError(f"{name}.time: stops must follow the daily cycle without duplicates")
        compiled.append((minute, rgb(stop["color"], f"{name}.color")))
    temperature = dict(appearance["temperature"])
    for key in temperature:
        if key not in DEFAULT_APPEARANCE["temperature"]:
            raise ValueError(f"appearance.temperature.{key}: unknown setting")
    for key in ("cold_c", "neutral_c", "hot_c"):
        temperature[key] = number(temperature[key], -100, 100, f"appearance.temperature.{key}")
    if not temperature["cold_c"] < temperature["neutral_c"] < temperature["hot_c"]:
        raise ValueError("appearance.temperature: require cold_c < neutral_c < hot_c")
    temperature["max_mix"] = number(temperature["max_mix"], 0, 1, "appearance.temperature.max_mix")
    for key in ("cold_color", "hot_color"):
        temperature[key] = rgb(temperature[key], f"appearance.temperature.{key}")
    return {
        "timezone": parsed_zone,
        "color_stops": tuple(compiled),
        "temperature": temperature,
        "rest_icon": private_codepoint(appearance["rest_icon"], "appearance.rest_icon"),
    }


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


def weather_kind(code, is_day):
    if type(code) is not int or type(is_day) is not int or is_day not in (0, 1):
        return None
    if code in (0, 1):
        name = "sun" if is_day else "moon"
    elif code == 2:
        name = "partly_day" if is_day else "partly_night"
    elif code == 3:
        name = "cloud"
    elif code in (45, 48):
        name = "fog_day" if is_day else "fog_night"
    elif code in (51, 53, 55, 61, 63, 80, 81):
        name = "rain_day" if is_day else "rain_night"
    elif code in (65, 82):
        name = "pouring"
    elif code in (56, 57, 66, 67):
        name = "sleet_day" if is_day else "sleet_night"
    elif code in (71, 73, 75, 77, 85, 86):
        name = "snow"
    elif code in (95, 96, 97, 99):
        name = "storm"
    else:
        return None
    return name


def icon_for(config, kind, seed):
    """Stable for a cache revision, independent of Python's randomized hash()."""
    candidates = config["icons"][kind]
    codes = ",".join(f"{point:x}" for point in candidates)
    identity = f'{config["latitude"]},{config["longitude"]}:{seed}:{kind}:{codes}'
    choice = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")
    return candidates[choice % len(candidates)]


def mix_color(first, second, fraction):
    return tuple(round(a + (b - a) * fraction) for a, b in zip(first, second))


def cycle_minute(appearance, now):
    local = datetime.fromtimestamp(now, appearance["timezone"])
    minute = local.hour * 60 + local.minute + local.second / 60
    start = appearance["color_stops"][0][0]
    return start + (minute - start) % 1440


def is_rest(config, now):
    appearance = config["appearance"]
    return cycle_minute(appearance, now) >= appearance["color_stops"][-1][0]


def color_at(config, now, temperature_c):
    appearance = config["appearance"]
    minute = cycle_minute(appearance, now)
    stops = appearance["color_stops"]
    if minute >= stops[-1][0]:
        color = stops[-1][1]
    else:
        for (start, first), (end, second) in zip(stops, stops[1:]):
            if start <= minute < end:
                color = mix_color(first, second, (minute - start) / (end - start))
                break
        temp = config["appearance"]["temperature"]
        delta = temperature_c - temp["neutral_c"]
        target = temp["cold_color"] if delta < 0 else temp["hot_color"]
        span = temp["neutral_c"] - temp["cold_c"] if delta < 0 else temp["hot_c"] - temp["neutral_c"]
        amount = temp["max_mix"] * min(1, abs(delta) / span)
        # The final segment (22:00-02:00 by default) is the rest transition:
        # fade both the base color and temperature influence toward rest.
        fade = min(1, (stops[-1][0] - minute) / (stops[-1][0] - stops[-2][0]))
        color = mix_color(color, target, amount * fade)
    return "#" + "".join(f"{channel:02x}" for channel in color)


def validated_current(value):
    if not isinstance(value, dict):
        raise ValueError("missing current conditions")
    if weather_kind(value.get("weather_code"), value.get("is_day")) is None:
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
    if is_rest(config, now):
        point = config["appearance"]["rest_icon"]
        return f'{color_at(config, now, 0)}\t{point:x}'
    if age(data, "fetched_at", now) > config["max_stale_seconds"]:
        return ""
    try:
        current = validated_current(data.get("current"))
    except (ValueError, TypeError):
        return ""
    kind = weather_kind(current["weather_code"], current["is_day"])
    color = color_at(config, now, current["temperature_2m"])
    return color + "\t" + f'{icon_for(config, kind, data["fetched_at"]):x}'


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
    # Rest is independent of weather availability; resume refresh on waking.
    if is_rest(config, now):
        return display(config, {}, now)
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


def preview_times(appearance):
    """Sample configured stops, segment midpoints, and the middle of rest."""
    minutes = [minute for minute, _ in appearance["color_stops"]]
    samples = []
    for start, end in zip(minutes, minutes[1:] + [minutes[0] + 1440]):
        samples.extend((start, (start + end) // 2))
    return [f"{minute % 1440 // 60:02d}:{minute % 60:02d}"
            for minute in dict.fromkeys(samples)]


def preview(config, options):
    """Render synthetic samples, without reading cache or scheduling HTTP."""
    temp = number(options.temperature, -100, 100, "--temperature")
    times = [options.time] if options.time else preview_times(config["appearance"])
    today = datetime.now(config["appearance"]["timezone"])
    print(f'Offline preview | {config["appearance"]["timezone"]} | {temp:g} C')
    for clock in times:
        minute = minute_of_day(clock)
        instant = today.replace(hour=minute // 60, minute=minute % 60,
                                second=0, microsecond=0).timestamp()
        # Synthetic daylight only for this preview; live data uses is_day.
        kind = options.condition or ("sun" if 6 * 60 <= minute < 18 * 60 else "moon")
        color = color_at(config, instant, temp)
        channels = ";".join(str(channel) for channel in rgb(color))
        if is_rest(config, instant):
            kind = "rest"
            point = config["appearance"]["rest_icon"]
        else:
            point = icon_for(config, kind, options.seed)
        glyph = chr(point)
        print(f'{clock}  \x1b[1;38;2;{channels}m{glyph}\x1b[0m '
              f'\x1b[1;38;2;245;189;230m {clock}\x1b[0m  {color}  {kind}')
    # Also show every configured candidate, including alternatives not drawn.
    if not options.condition:
        print("\nConfigured icons:")
        candidates = dict(config["icons"], rest=(config["appearance"]["rest_icon"],))
        for kind, points in candidates.items():
            sample_color = (";".join(map(str, config["appearance"]["color_stops"][-1][1]))
                            if kind == "rest" else "198;160;246")
            samples = "  ".join(f'\x1b[1;38;2;{sample_color}m{chr(point)}\x1b[0m '
                                f'{point:x}' for point in points)
            print(f'{kind:14} {samples}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode")
    for mode_name in ("prompt", "refresh", "worker"):
        commands.add_parser(mode_name)
    preview_parser = commands.add_parser("preview", help="offline color and icon samples")
    preview_parser.add_argument("--time", help="simulate an HH:MM in the configured timezone")
    preview_parser.add_argument("--temperature", type=float, default=22)
    preview_parser.add_argument("--condition", choices=tuple(DEFAULT_ICONS))
    preview_parser.add_argument("--seed", type=int, default=0, help="change the candidate selection")
    options = parser.parse_args(argv)
    mode = options.mode or "prompt"
    locations = None
    locked = False
    try:
        config = settings(require_location=mode != "preview")
        if mode == "preview":
            sys.stdout.reconfigure(encoding="utf-8")
            preview(config, options)
            return 0
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
        if mode in ("refresh", "preview"):
            print(f"Weather {mode} failed: {error}", file=sys.stderr)
        return 0 if mode == "prompt" else 1
    finally:
        if locked and locations is not None:
            release_lock(locations["lock"])


if __name__ == "__main__":
    raise SystemExit(main())
