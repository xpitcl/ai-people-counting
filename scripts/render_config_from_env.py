#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import Any


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def env_int(name: str, default: int) -> int:
    value = env(name)
    return int(value) if value else default


def env_float(name: str, default: float) -> float:
    value = env(name)
    return float(value) if value else default


def env_bool(name: str, default: bool) -> bool:
    value = env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def parse_json_env(name: str) -> Any:
    value = os.environ.get(name)
    if not value:
        return None
    return json.loads(value)


def set_if_env(target: dict, key: str, env_name: str) -> None:
    value = env(env_name)
    if value:
        target[key] = value


def main() -> int:
    if len(sys.argv) != 3:
        print("Uso: render_config_from_env.py TEMPLATE_PATH CONFIG_PATH", file=sys.stderr)
        return 2

    template_path = Path(sys.argv[1])
    config_path = Path(sys.argv[2])

    existing_config = None
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            existing_config = json.load(f)

    config_json = parse_json_env("CONFIG_JSON")
    if config_json is not None:
        config = config_json
        if existing_config and env_bool("CONFIG_PRESERVE_STATE", True):
            config["state"] = existing_config.get("state", config.get("state", {}))
    elif existing_config and not env_bool("CONFIG_OVERWRITE", False):
        config = existing_config
    else:
        with template_path.open("r", encoding="utf-8") as f:
            config = json.load(f)

    settings = config.setdefault("settings", {})
    settings["interval_sec"] = env_int("INTERVAL_SEC", int(settings.get("interval_sec", 60)))
    settings["person_class_id"] = env_int("PERSON_CLASS_ID", int(settings.get("person_class_id", 0)))
    settings["min_detector_confidence"] = env_float(
        "MIN_DETECTOR_CONFIDENCE",
        float(settings.get("min_detector_confidence", 0.25)),
    )

    mqtt = settings.setdefault("mqtt", {})
    set_if_env(mqtt, "host", "MQTT_HOST")
    if env("MQTT_PORT"):
        mqtt["port"] = env_int("MQTT_PORT", int(mqtt.get("port", 1883)))
    set_if_env(mqtt, "username", "MQTT_USERNAME")
    set_if_env(mqtt, "password", "MQTT_PASSWORD")
    set_if_env(mqtt, "client_id", "MQTT_CLIENT_ID")
    set_if_env(mqtt, "topic_template", "MQTT_TOPIC_TEMPLATE")
    if env("MQTT_QOS"):
        mqtt["qos"] = env_int("MQTT_QOS", int(mqtt.get("qos", 1)))
    if env("MQTT_RETAIN"):
        mqtt["retain"] = env_bool("MQTT_RETAIN", bool(mqtt.get("retain", False)))

    config.setdefault("runtime", {})["backend"] = env("BACKEND") or config.get("runtime", {}).get("backend", "deepstream")

    deepstream = config.setdefault("deepstream", {})
    watchdog = deepstream.setdefault("watchdog", {})
    if env("DEEPSTREAM_WATCHDOG_ENABLED"):
        watchdog["enabled"] = env_bool("DEEPSTREAM_WATCHDOG_ENABLED", bool(watchdog.get("enabled", True)))
    if env("DEEPSTREAM_WATCHDOG_INITIAL_GRACE_SEC"):
        watchdog["initial_grace_sec"] = env_int(
            "DEEPSTREAM_WATCHDOG_INITIAL_GRACE_SEC",
            int(watchdog.get("initial_grace_sec", 600)),
        )
    if env("DEEPSTREAM_WATCHDOG_STALL_SEC"):
        watchdog["stall_sec"] = env_int("DEEPSTREAM_WATCHDOG_STALL_SEC", int(watchdog.get("stall_sec", 120)))
    if env("DEEPSTREAM_WATCHDOG_CHECK_INTERVAL_SEC"):
        watchdog["check_interval_sec"] = env_int(
            "DEEPSTREAM_WATCHDOG_CHECK_INTERVAL_SEC",
            int(watchdog.get("check_interval_sec", 10)),
        )

    processed_rtsp = deepstream.setdefault("processed_rtsp", {})
    if env("PROCESSED_RTSP_ENABLED"):
        processed_rtsp["enabled"] = env_bool("PROCESSED_RTSP_ENABLED", bool(processed_rtsp.get("enabled", False)))
    set_if_env(processed_rtsp, "url", "PROCESSED_RTSP_URL")
    if env("PROCESSED_RTSP_WIDTH"):
        processed_rtsp["width"] = env_int("PROCESSED_RTSP_WIDTH", int(processed_rtsp.get("width", 640)))
    if env("PROCESSED_RTSP_HEIGHT"):
        processed_rtsp["height"] = env_int("PROCESSED_RTSP_HEIGHT", int(processed_rtsp.get("height", 360)))
    if env("PROCESSED_RTSP_FPS"):
        processed_rtsp["fps"] = env_int("PROCESSED_RTSP_FPS", int(processed_rtsp.get("fps", 5)))
    if env("PROCESSED_RTSP_BITRATE"):
        processed_rtsp["bitrate"] = env_int("PROCESSED_RTSP_BITRATE", int(processed_rtsp.get("bitrate", 1_000_000)))

    cameras_json = parse_json_env("CAMERAS_JSON")
    if cameras_json is not None:
        config["cameras"] = cameras_json

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=True)
    os.replace(tmp_path, config_path)
    print(f"[INFO] Config listo: {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
