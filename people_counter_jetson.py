#!/usr/bin/env python3
"""
People counter for Jetson Orin Nano using NVIDIA DeepStream or OpenCV + MQTT.

Features:
- Multi-RTSP ingest (4+ cameras supported).
- Interactive line calibration per camera (saved in JSON config).
- In/Out/Inside counting per camera based on line crossing direction.
- Tiled display with per-camera counters.
- MQTT publish every minute, with persistent offline queue/retry.
- Optional per-person attributes from DeepStream classifier metadata (DeepStream backend).

Usage:
1) Create template config:
   python3 people_counter_jetson.py --init

2) Edit RTSP URIs and MQTT server in config.json.

3) Calibrate lines:
   python3 people_counter_jetson.py --calibrate

4) Run counter:
   python3 people_counter_jetson.py

5) Optional backend override:
   python3 people_counter_jetson.py --backend opencv
"""

import argparse
import datetime as dt
import json
import math
import os
import platform
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import cv2
except Exception:
    cv2 = None

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

try:
    import numpy as np
except Exception:
    np = None

pyds = None


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_vec(v: List[float]) -> Tuple[float, float]:
    x, y = float(v[0]), float(v[1])
    n = math.hypot(x, y)
    if n < 1e-6:
        return (0.0, -1.0)
    return (x / n, y / n)


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
    os.replace(tmp, path)


def first_existing_path(candidates: List[str]) -> Optional[str]:
    for p in candidates:
        if p and Path(p).exists():
            return p
    return None


def default_config() -> dict:
    return {
        "settings": {
            "interval_sec": 60,
            "person_class_id": 2,
            "min_crossing_gap_frames": 12,
            "track_max_idle_frames": 120,
            "line_deadband_px": 3.0,
            "source_width": 1280,
            "source_height": 720,
            "streammux_batch_timeout_us": 40000,
            "min_detector_confidence": 0.25,
            "mqtt": {
                "host": "YOUR_MQTT_HOST",
                "port": 1883,
                "topic_template": "location/{location_id}/device/{device_id}/event/up",
                "client_id": "jetson-orin-nano-counter",
                "username": "",
                "password": "",
                "qos": 1,
                "retain": False,
                "keepalive": 30,
            },
        },
        "runtime": {"backend": "deepstream"},
        "opencv": {
            "detector": "hog",
            "detect_every_n_frames": 4,
            "detect_resize_width": 640,
            "detect_resize_height": 360,
            "hog_win_stride": [8, 8],
            "hog_padding": [8, 8],
            "hog_scale": 1.05,
            "hog_hit_threshold": 0.0,
            "hog_min_confidence": 0.0,
            "tracker_max_disappeared_frames": 20,
            "tracker_max_match_distance_px": 80.0,
        },
        "deepstream": {
            "pgie_config_path": "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_infer_primary.txt",
            "tracker": {
                "tracker-width": 640,
                "tracker-height": 384,
                "gpu-id": 0,
                "ll-lib-file": "/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_nvmultiobjecttracker.so",
                "ll-config-file": "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml",
                "enable-batch-process": 1,
                "enable-past-frame": 0,
            },
        },
        "cameras": [
            {
                "id": "cam_1",
                "name": "Camara 1",
                "location_id": "no_code_location",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"id": "cam_1_line_1", "p1": [200, 360], "p2": [1080, 360], "in_direction": [0, -1]},
            },
            {
                "id": "cam_2",
                "name": "Camara 2",
                "location_id": "no_code_location",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"id": "cam_2_line_1", "p1": [640, 100], "p2": [640, 620], "in_direction": [1, 0]},
            },
            {
                "id": "cam_3",
                "name": "Camara 3",
                "location_id": "no_code_location",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"id": "cam_3_line_1", "p1": [220, 130], "p2": [1000, 600], "in_direction": [0, 1]},
            },
            {
                "id": "cam_4",
                "name": "Camara 4",
                "location_id": "no_code_location",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"id": "cam_4_line_1", "p1": [300, 100], "p2": [300, 700], "in_direction": [-1, 0]},
            },
        ],
        "state": {"totals": {}, "pending_mqtt": [], "last_minute_payload_utc": None},
    }


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {}

    def load(self) -> None:
        with self.lock:
            with self.path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
            self._ensure_runtime_defaults()

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.data)

    def create_template(self) -> None:
        with self.lock:
            self.data = default_config()
            self._ensure_runtime_defaults()
            atomic_write_json(self.path, self.data)

    def _ensure_runtime_defaults(self) -> None:
        self.data.setdefault("settings", {})
        self.data.setdefault("runtime", {})
        self.data.setdefault("opencv", {})
        self.data.setdefault("deepstream", {})
        self.data.setdefault("cameras", [])
        self.data.setdefault("state", {})
        self.data["settings"].setdefault("mqtt", {})

        self.data["runtime"].setdefault("backend", "deepstream")
        self.data["opencv"].setdefault("detector", "hog")
        self.data["opencv"].setdefault("detect_every_n_frames", 4)
        self.data["opencv"].setdefault("detect_resize_width", 640)
        self.data["opencv"].setdefault("detect_resize_height", 360)
        self.data["opencv"].setdefault("hog_win_stride", [8, 8])
        self.data["opencv"].setdefault("hog_padding", [8, 8])
        self.data["opencv"].setdefault("hog_scale", 1.05)
        self.data["opencv"].setdefault("hog_hit_threshold", 0.0)
        self.data["opencv"].setdefault("hog_min_confidence", 0.0)
        self.data["opencv"].setdefault("tracker_max_disappeared_frames", 20)
        self.data["opencv"].setdefault("tracker_max_match_distance_px", 80.0)
        self.data["settings"]["mqtt"].pop("topic", None)
        self.data["settings"]["mqtt"].setdefault("topic_template", "location/{location_id}/device/{device_id}/event/up")

        self.data["state"].setdefault("totals", {})
        self.data["state"].setdefault("pending_mqtt", [])
        self.data["state"].setdefault("last_minute_payload_utc", None)

        for idx, cam in enumerate(self.data["cameras"]):
            cam.setdefault("id", f"cam_{idx+1}")
            cam.setdefault("name", cam["id"])
            cam.setdefault("line", {})
            cam.setdefault("location_id", "no_code_location")
            if "line_id" in cam["line"] and "id" not in cam["line"]:
                cam["line"]["id"] = cam["line"]["line_id"]
            cam["line"].setdefault("p1", [200, 200])
            cam["line"].setdefault("p2", [1000, 200])
            cam["line"].setdefault("in_direction", [0, -1])
            cam_id = str(cam["id"])
            cam["line"].setdefault("id", f"{cam_id}_line_1")
            self.data["state"]["totals"].setdefault(cam_id, {"in": 0, "out": 0})


class MQTTPublisher:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.client = None
        self.connected = False
        self.lock = threading.RLock()

    def start(self) -> None:
        if mqtt is None:
            raise RuntimeError("Falta dependencia: paho-mqtt (pip3 install paho-mqtt)")
        cfg = self.store.data["settings"]["mqtt"]
        self.client = mqtt.Client(client_id=cfg.get("client_id") or "", clean_session=True)
        username = cfg.get("username") or ""
        password = cfg.get("password") or ""
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.connect_async(cfg["host"], int(cfg.get("port", 1883)), int(cfg.get("keepalive", 30)))
        self.client.loop_start()

    def stop(self) -> None:
        with self.lock:
            if self.client is None:
                return
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self.connected = False

    def _on_connect(self, client, userdata, flags, rc):
        with self.lock:
            self.connected = (rc == 0)
        if self.connected:
            self.flush_pending()

    def _on_disconnect(self, client, userdata, rc):
        with self.lock:
            self.connected = False

    def _normalize_message(self, message: dict) -> dict:
        cfg = self.store.data["settings"]["mqtt"]
        topic_template = str(cfg.get("topic_template") or "location/{location_id}/device/{device_id}/event/up").strip()
        default_topic = topic_template
        default_topic = default_topic.replace("{location_id}", "no_code_location")
        default_topic = default_topic.replace("{locationID}", "no_code_location")
        default_topic = default_topic.replace("{device_id}", "unknown_device")
        default_topic = default_topic.replace("{deviceID}", "unknown_device")
        default_topic = default_topic.replace("{camera_id}", "unknown_device")
        if isinstance(message, dict) and "payload" in message:
            payload = message.get("payload", {})
            topic = message.get("topic", default_topic)
        else:
            payload = message
            topic = default_topic
        return {"topic": str(topic), "payload": payload}

    def _publish(self, message: dict) -> bool:
        cfg = self.store.data["settings"]["mqtt"]
        qos = int(cfg.get("qos", 1))
        retain = bool(cfg.get("retain", False))
        normalized = self._normalize_message(message)
        topic = normalized["topic"]
        body = json.dumps(normalized["payload"], ensure_ascii=True)
        with self.lock:
            if not self.connected or self.client is None:
                return False
            info = self.client.publish(topic, body, qos=qos, retain=retain)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        if ok:
            try:
                info.wait_for_publish(timeout=2.0)
            except Exception:
                return False
        return ok

    def enqueue_pending(self, message: dict) -> None:
        normalized = self._normalize_message(message)
        with self.store.lock:
            self.store.data["state"]["pending_mqtt"].append(normalized)
            self.store.save()

    def flush_pending(self) -> None:
        with self.store.lock:
            pending = self.store.data["state"]["pending_mqtt"]
            if not pending:
                return
            remaining = []
            for message in pending:
                normalized = self._normalize_message(message)
                if not self._publish(normalized):
                    remaining.append(normalized)
            self.store.data["state"]["pending_mqtt"] = remaining
            self.store.save()

    def publish_or_queue(self, message: dict) -> None:
        self.flush_pending()
        normalized = self._normalize_message(message)
        if not self._publish(normalized):
            self.enqueue_pending(normalized)


class CounterCore:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.track_state: Dict[str, Dict[int, dict]] = {}
        self.minute_counts: Dict[str, Dict[str, int]] = {}
        self.minute_events: List[dict] = []
        self.hostname = socket.gethostname()
        self._init_runtime_maps()

    def _init_runtime_maps(self) -> None:
        for cam in self.store.data["cameras"]:
            cam_id = str(cam["id"])
            self.track_state.setdefault(cam_id, {})
            self.minute_counts.setdefault(cam_id, {"in": 0, "out": 0})
            self.store.data["state"]["totals"].setdefault(cam_id, {"in": 0, "out": 0})

    def get_totals(self, cam_id: str) -> dict:
        return self.store.data["state"]["totals"].setdefault(cam_id, {"in": 0, "out": 0})

    def inside(self, cam_id: str) -> int:
        t = self.get_totals(cam_id)
        return int(t["in"]) - int(t["out"])

    def _camera_line_id(self, cam: dict) -> str:
        cam_id = str(cam.get("id", "cam"))
        line_cfg = cam.get("line", {}) or {}
        line_id = line_cfg.get("id") or line_cfg.get("line_id")
        if not line_id:
            line_id = f"{cam_id}_line_1"
        return str(line_id)

    def _camera_location_id(self, cam: dict) -> str:
        return str(
            cam.get("location_id")
            or cam.get("locationID")
            or "no_code_location"
        )

    def _topic_for_camera(self, cam: dict) -> str:
        mqtt_cfg = self.store.data["settings"].get("mqtt", {})
        template = str(mqtt_cfg.get("topic_template") or "location/{location_id}/device/{device_id}/event/up").strip()

        location_id = self._camera_location_id(cam)
        device_id = str(cam.get("id", "cam"))
        topic = template
        topic = topic.replace("{location_id}", location_id)
        topic = topic.replace("{locationID}", location_id)
        topic = topic.replace("{device_id}", device_id)
        topic = topic.replace("{deviceID}", device_id)
        topic = topic.replace("{camera_id}", device_id)
        return topic

    def _line_side(self, p1, p2, pt, deadband_px: float) -> int:
        x1, y1 = p1
        x2, y2 = p2
        x, y = pt
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        line_len = max(math.hypot(x2 - x1, y2 - y1), 1.0)
        signed_dist = cross / line_len
        if abs(signed_dist) <= deadband_px:
            return 0
        return 1 if signed_dist > 0 else -1

    def _extract_attrs(self, obj_meta) -> Dict[str, str]:
        attrs: Dict[str, str] = {}
        if obj_meta is None or pyds is None:
            return attrs
        c_list = obj_meta.classifier_meta_list
        while c_list is not None:
            try:
                c_meta = pyds.NvDsClassifierMeta.cast(c_list.data)
                label_list = c_meta.label_info_list
                while label_list is not None:
                    try:
                        l_meta = pyds.NvDsLabelInfo.cast(label_list.data)
                        if l_meta.result_label:
                            key = f"classifier_{int(c_meta.unique_component_id)}"
                            attrs[key] = str(l_meta.result_label)
                        label_list = label_list.next
                    except StopIteration:
                        break
                c_list = c_list.next
            except StopIteration:
                break
        return attrs

    def _is_person_meta(self, obj_meta, person_class_id: int) -> bool:
        if obj_meta is None:
            return False
        if int(obj_meta.class_id) != int(person_class_id):
            return False

        # Some models expose class_id only; others also set a text label.
        label = str(getattr(obj_meta, "obj_label", "") or "").strip().lower()
        if not label:
            return True

        return any(token in label for token in ("person", "persona", "pedestrian", "people"))

    def process_object(
        self,
        cam: dict,
        frame_num: int,
        object_id: int,
        centroid: Tuple[float, float],
        obj_meta=None,
    ) -> None:
        settings = self.store.data["settings"]
        cam_id = str(cam["id"])
        p1 = tuple(cam["line"]["p1"])
        p2 = tuple(cam["line"]["p2"])
        in_vec = normalize_vec(cam["line"]["in_direction"])
        line_vec = (p2[0] - p1[0], p2[1] - p1[1])
        line_normal = normalize_vec((-line_vec[1], line_vec[0]))  # left normal of p1->p2
        in_normal_dot = line_normal[0] * in_vec[0] + line_normal[1] * in_vec[1]
        deadband = float(settings.get("line_deadband_px", 3.0))
        min_gap = int(settings.get("min_crossing_gap_frames", 12))

        side = self._line_side(p1, p2, centroid, deadband)
        state = self.track_state[cam_id].setdefault(
            object_id,
            {
                "prev_pt": None,
                "prev_side": 0,
                "last_nonzero_side": 0,
                "last_cross_frame": -10_000,
                "last_seen": frame_num,
            },
        )
        prev_pt = state["prev_pt"]
        prev_nonzero_side = int(state.get("last_nonzero_side", 0))

        if prev_pt is not None and prev_nonzero_side != 0 and side != 0 and prev_nonzero_side * side < 0:
            if frame_num - int(state["last_cross_frame"]) >= min_gap:
                direction = None
                # Prefer side-transition classification (stable) when in_direction has a
                # meaningful component orthogonal to the counting line.
                if abs(in_normal_dot) > 0.2:
                    in_side = 1 if in_normal_dot > 0 else -1
                    direction = "in" if side == in_side else "out"
                else:
                    # Fallback when in_direction is near-parallel to the line.
                    mv = (centroid[0] - prev_pt[0], centroid[1] - prev_pt[1])
                    dot = mv[0] * in_vec[0] + mv[1] * in_vec[1]
                    if abs(dot) > 1e-6:
                        direction = "in" if dot > 0 else "out"

                if direction is not None:
                    self.get_totals(cam_id)[direction] += 1
                    self.minute_counts[cam_id][direction] += 1
                    state["last_cross_frame"] = frame_num
                    self.minute_events.append(
                        {
                            "ts_utc": utc_now_iso(),
                            "camera_id": cam_id,
                            "camera_name": cam.get("name", cam_id),
                            "line_id": self._camera_line_id(cam),
                            "track_id": int(object_id),
                            "direction": direction,
                            "attrs": self._extract_attrs(obj_meta),
                        }
                    )

        state["prev_pt"] = centroid
        state["prev_side"] = side
        if side != 0:
            state["last_nonzero_side"] = side
        state["last_seen"] = frame_num

    def prune_tracks(self, cam_id: str, frame_num: int) -> None:
        max_idle = int(self.store.data["settings"].get("track_max_idle_frames", 120))
        tmap = self.track_state[cam_id]
        stale = [tid for tid, st in tmap.items() if frame_num - int(st.get("last_seen", frame_num)) > max_idle]
        for tid in stale:
            del tmap[tid]

    def build_interval_messages(self) -> List[dict]:
        interval_sec = int(self.store.data["settings"].get("interval_sec", 60))
        now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        window_end_iso = now_dt.isoformat().replace("+00:00", "Z")
        window_start_iso = (now_dt - dt.timedelta(seconds=interval_sec)).isoformat().replace("+00:00", "Z")
        messages: List[dict] = []

        for cam in self.store.data["cameras"]:
            cam_id = str(cam["id"])
            line_id = self._camera_line_id(cam)
            location_id = self._camera_location_id(cam)
            totals = self.get_totals(cam_id)
            minute = self.minute_counts[cam_id]
            occupancy = int(totals["in"]) - int(totals["out"])
            topic = self._topic_for_camera(cam)

            payload = {
                "schema_version": 1,
                "message_type": "metric",
                "msg_id": str(uuid.uuid4()),
                "time": window_end_iso,
                "window_start": window_start_iso,
                "window_end": window_end_iso,
                "protocol": "Vision",
                "deviceID": cam_id,
                "locationID": location_id,
                "device_info": {
                    "friendly_name": cam.get("name", cam_id),
                    "manufacturer_name": cam.get("manufacturer_name", "NVIDIA"),
                    "device_model": cam.get("device_model", "Jetson Orin Nano"),
                    "device_type": cam.get("device_type", "CameraEdge"),
                    "host": self.hostname,
                },
                "object": {
                    "line_id": line_id,
                    "people_in_interval": int(minute["in"]),
                    "people_out_interval": int(minute["out"]),
                    "people_in_total": int(totals["in"]),
                    "people_out_total": int(totals["out"]),
                    "people_occupancy": occupancy,
                    "count_in": int(minute["in"]),
                    "count_out": int(minute["out"]),
                    "inside": occupancy,
                    "total_in": int(totals["in"]),
                    "total_out": int(totals["out"]),
                    "interval_sec": interval_sec,
                },
            }
            messages.append({"topic": topic, "payload": payload})

        return messages

    def reset_minute(self) -> None:
        for v in self.minute_counts.values():
            v["in"] = 0
            v["out"] = 0
        self.minute_events = []


def require_runtime_modules() -> None:
    if cv2 is None:
        raise RuntimeError("Falta dependencia: opencv-python o python3-opencv")
    try:
        import gi  # noqa: F401
        import pyds as pyds_check  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "No se pudo importar GStreamer/DeepStream Python bindings (gi + pyds). "
            "Asegura tener DeepStream instalado en Jetson."
        ) from e

    # Verifica API minima esperada por el backend DeepStream.
    if not hasattr(pyds_check, "gst_buffer_get_nvds_batch_meta"):
        mod_path = getattr(pyds_check, "__file__", "<desconocido>")
        raise RuntimeError(
            "El modulo pyds cargado no es compatible con DeepStream Python API. "
            f"Modulo cargado: {mod_path}. "
            "Probablemente se cargo un pyds de pip en vez del binding de DeepStream."
        )


def draw_line_ui(frame, cam_label: str, initial_line: dict) -> Optional[dict]:
    points = []
    if initial_line and "p1" in initial_line and "p2" in initial_line:
        points = [tuple(initial_line["p1"]), tuple(initial_line["p2"])]
    in_dir = tuple(initial_line.get("in_direction", [0, -1])) if initial_line else (0, -1)
    line_id = str(initial_line.get("id") or initial_line.get("line_id") or "line_1") if initial_line else "line_1"
    line_done = False
    win_name = f"Calibracion - {cam_label}"

    def mouse_cb(event, x, y, flags, param):
        nonlocal points, line_done, in_dir
        if event == cv2.EVENT_LBUTTONDOWN:
            if len(points) >= 2:
                points = []
            points.append((int(x), int(y)))
            line_done = len(points) == 2
        elif event == cv2.EVENT_RBUTTONDOWN and len(points) == 2:
            cx = int((points[0][0] + points[1][0]) / 2)
            cy = int((points[0][1] + points[1][1]) / 2)
            dx = int(x) - cx
            dy = int(y) - cy
            if abs(dx) + abs(dy) > 0:
                in_dir = (dx, dy)

    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win_name, mouse_cb)

    while True:
        canvas = frame.copy()
        h, w = canvas.shape[:2]

        if len(points) >= 1:
            cv2.circle(canvas, points[0], 6, (0, 255, 255), -1)
        if len(points) == 2:
            cv2.circle(canvas, points[1], 6, (0, 255, 255), -1)
            cv2.line(canvas, points[0], points[1], (0, 255, 0), 2)
            cx = int((points[0][0] + points[1][0]) / 2)
            cy = int((points[0][1] + points[1][1]) / 2)
            arrow_len = 80
            vec = normalize_vec([in_dir[0], in_dir[1]])
            p2 = (int(cx + vec[0] * arrow_len), int(cy + vec[1] * arrow_len))
            cv2.arrowedLine(canvas, (cx, cy), p2, (255, 200, 0), 3, tipLength=0.25)

        tips = [
            "Click izquierdo: marca P1 y P2 de la linea",
            "Direccion IN: W(arriba) A(izq) S(abajo) D(der)",
            "Click derecho: direccion IN personalizada",
            "R: reset linea | Enter: guardar camara | ESC: cancelar",
        ]
        y = 30
        for txt in tips:
            cv2.putText(canvas, txt, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
            y += 28

        cv2.putText(
            canvas,
            f"Resolucion calibracion: {w}x{h}",
            (10, h - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow(win_name, canvas)
        key = cv2.waitKey(20) & 0xFF
        if key in (27,):  # ESC
            cv2.destroyWindow(win_name)
            return None
        if key in (ord("r"), ord("R")):
            points = []
            line_done = False
        elif key in (ord("w"), ord("W")):
            in_dir = (0, -1)
        elif key in (ord("a"), ord("A")):
            in_dir = (-1, 0)
        elif key in (ord("s"), ord("S")):
            in_dir = (0, 1)
        elif key in (ord("d"), ord("D")):
            in_dir = (1, 0)
        elif key in (13, 10):  # Enter
            if line_done:
                cv2.destroyWindow(win_name)
                return {
                    "id": line_id,
                    "p1": [points[0][0], points[0][1]],
                    "p2": [points[1][0], points[1][1]],
                    "in_direction": [in_dir[0], in_dir[1]],
                }


def calibrate_lines(store: ConfigStore) -> None:
    if cv2 is None:
        raise RuntimeError("Falta dependencia: python3-opencv")

    cams = store.data.get("cameras", [])
    if not cams:
        raise RuntimeError("No hay camaras en el archivo de configuracion.")

    w = int(store.data["settings"].get("source_width", 1280))
    h = int(store.data["settings"].get("source_height", 720))

    print("Calibracion iniciada. Se abrira una ventana por camara.")
    print("Tip: presiona Enter para guardar cada camara.")

    def _capture_frame(uri: str):
        backends = []
        if hasattr(cv2, "CAP_FFMPEG"):
            backends.append(("ffmpeg", cv2.CAP_FFMPEG, uri))
        if hasattr(cv2, "CAP_GSTREAMER"):
            backends.append(("gstreamer-uri", cv2.CAP_GSTREAMER, uri))
            gst_uri = uri.replace("\\", "\\\\").replace('"', '\\"')
            gst_pipelines = [
                f'rtspsrc location="{gst_uri}" latency=200 protocols=tcp drop-on-latency=true ! '
                "rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! "
                "video/x-raw,format=BGR ! appsink sync=false drop=true max-buffers=1",
                f'rtspsrc location="{gst_uri}" latency=200 protocols=tcp drop-on-latency=true ! '
                "rtph265depay ! h265parse ! avdec_h265 ! videoconvert ! "
                "video/x-raw,format=BGR ! appsink sync=false drop=true max-buffers=1",
            ]
            for pipe in gst_pipelines:
                backends.append(("gstreamer-pipe", cv2.CAP_GSTREAMER, pipe))
        backends.append(("default", None, uri))

        best = None
        for bname, api, source in backends:
            cap = cv2.VideoCapture(source, api) if api is not None else cv2.VideoCapture(source)
            if not cap.isOpened():
                cap.release()
                continue
            candidate = None
            for _ in range(120):
                ok, fr = cap.read()
                if not ok or fr is None:
                    time.sleep(0.03)
                    continue
                # Evita elegir frame casi plano/gris cuando OpenCV entrega basura de decodificacion.
                std = cv2.meanStdDev(fr)[1]
                avg_std = float(std.mean())
                if avg_std > 2.0:
                    candidate = fr
                    break
                if candidate is None:
                    candidate = fr
            cap.release()
            if candidate is not None:
                best = (candidate, bname)
                if bname != "default":
                    return best
        return best

    for cam in cams:
        uri = cam.get("uri", "")
        if not uri or "rtsp://" not in uri:
            print(f"[WARN] Saltando {cam.get('name', cam.get('id'))}: URI RTSP invalida.")
            continue

        grabbed = _capture_frame(uri)
        frame = grabbed[0] if grabbed else None
        backend_name = grabbed[1] if grabbed else "none"

        if frame is None:
            print(f"[WARN] Sin frame para {cam.get('name', cam.get('id'))}.")
            continue
        print(f"[INFO] {cam.get('name', cam.get('id'))}: frame obtenido con backend {backend_name}.")

        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        line = draw_line_ui(frame, cam.get("name", cam.get("id")), cam.get("line", {}))
        if line is None:
            print("Calibracion cancelada por usuario.")
            break
        cam["line"] = line
        print(f"[OK] Linea guardada para {cam.get('name', cam.get('id'))}: {line}")
        store.save()

    cv2.destroyAllWindows()
    print("Calibracion finalizada.")


def _set_if_prop_exists(elem, key: str, value) -> None:
    if elem.find_property(key) is not None:
        elem.set_property(key, value)


def _link_elements_or_raise(*elements) -> None:
    for i in range(len(elements) - 1):
        a = elements[i]
        b = elements[i + 1]
        if not a.link(b):
            raise RuntimeError(f"Error linkeando: {a.name} -> {b.name}")


def create_source_bin(index, uri):
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    bin_name = f"source-bin-{index}"
    nbin = Gst.Bin.new(bin_name)
    if nbin is None:
        raise RuntimeError("No se pudo crear source bin")

    uri_decode_bin = Gst.ElementFactory.make("uridecodebin", f"uri-decode-bin-{index}")
    if uri_decode_bin is None:
        raise RuntimeError("No se pudo crear uridecodebin")
    uri_decode_bin.set_property("uri", uri)

    def decodebin_child_added(child_proxy, obj, name, user_data):
        if "decodebin" in name:
            obj.connect("child-added", decodebin_child_added, user_data)
        if "source" in name:
            _set_if_prop_exists(obj, "drop-on-latency", True)
            _set_if_prop_exists(obj, "latency", 200)

    def cb_newpad(decodebin, decoder_src_pad, data):
        caps = decoder_src_pad.get_current_caps()
        if not caps:
            return
        structure = caps.get_structure(0)
        name = structure.get_name()
        if "video" not in name:
            return
        features = caps.get_features(0)
        if features and features.contains("memory:NVMM"):
            ghost_pad = data.get_static_pad("src")
            if not ghost_pad.set_target(decoder_src_pad):
                raise RuntimeError("No se pudo linkear decoder src pad al ghost pad")

    uri_decode_bin.connect("pad-added", cb_newpad, nbin)
    uri_decode_bin.connect("child-added", decodebin_child_added, nbin)
    nbin.add(uri_decode_bin)

    ghost_pad = Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC)
    if ghost_pad is None:
        raise RuntimeError("No se pudo crear ghost pad")
    nbin.add_pad(ghost_pad)
    return nbin


def bus_call(bus, message, loop):
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    mtype = message.type
    if mtype == Gst.MessageType.EOS:
        print("EOS recibido.")
        loop.quit()
    elif mtype == Gst.MessageType.ERROR:
        err, dbg = message.parse_error()
        print(f"[ERROR] {err}: {dbg}")
        loop.quit()
    elif mtype == Gst.MessageType.WARNING:
        err, dbg = message.parse_warning()
        print(f"[WARN] {err}: {dbg}")
    return True


def _read_pgie_key(config_path: str, key: str) -> Optional[str]:
    try:
        cfg = Path(config_path)
        base = cfg.parent
        with cfg.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    val = v.strip().strip('"').strip("'")
                    if not val:
                        return None
                    p = Path(val)
                    if not p.is_absolute():
                        p = (base / p).resolve()
                    return str(p)
    except Exception:
        return None
    return None


def _open_capture_with_fallback(uri: str):
    if cv2 is None:
        return (None, "none")

    backends = []
    if hasattr(cv2, "CAP_FFMPEG"):
        backends.append(("ffmpeg", cv2.CAP_FFMPEG, uri))
    if hasattr(cv2, "CAP_GSTREAMER"):
        backends.append(("gstreamer-uri", cv2.CAP_GSTREAMER, uri))
        gst_uri = uri.replace("\\", "\\\\").replace('"', '\\"')
        gst_pipelines = [
            f'rtspsrc location="{gst_uri}" latency=200 protocols=tcp drop-on-latency=true ! '
            "rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! "
            "video/x-raw,format=BGR ! appsink sync=false drop=true max-buffers=1",
            f'rtspsrc location="{gst_uri}" latency=200 protocols=tcp drop-on-latency=true ! '
            "rtph265depay ! h265parse ! avdec_h265 ! videoconvert ! "
            "video/x-raw,format=BGR ! appsink sync=false drop=true max-buffers=1",
        ]
        for pipe in gst_pipelines:
            backends.append(("gstreamer-pipe", cv2.CAP_GSTREAMER, pipe))
    backends.append(("default", None, uri))

    for bname, api, source in backends:
        cap = cv2.VideoCapture(source, api) if api is not None else cv2.VideoCapture(source)
        if cap.isOpened():
            return (cap, bname)
        cap.release()
    return (None, "none")


def _blank_frame(w: int, h: int, text: str):
    if np is None:
        raise RuntimeError("Falta dependencia: numpy para backend OpenCV")
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:] = (35, 35, 35)
    cv2.putText(frame, text, (20, int(h / 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (220, 220, 220), 2, cv2.LINE_AA)
    return frame


def _draw_overlay_opencv(frame, cam: dict, core: CounterCore):
    cam_id = str(cam["id"])
    totals = core.get_totals(cam_id)
    inside = int(totals["in"]) - int(totals["out"])
    text = f"{cam.get('name', cam_id)} | In:{int(totals['in'])} Out:{int(totals['out'])} Inside:{inside}"

    line = cam.get("line", {})
    p1 = tuple(line.get("p1", [100, 100]))
    p2 = tuple(line.get("p2", [400, 100]))
    in_dir = normalize_vec(line.get("in_direction", [0, -1]))

    cv2.line(frame, p1, p2, (0, 255, 0), 2)
    cx = int((p1[0] + p2[0]) / 2)
    cy = int((p1[1] + p2[1]) / 2)
    arrow_len = 70
    arrow_p2 = (int(cx + in_dir[0] * arrow_len), int(cy + in_dir[1] * arrow_len))
    cv2.arrowedLine(frame, (cx, cy), arrow_p2, (255, 200, 0), 2, tipLength=0.25)

    cv2.rectangle(frame, (8, 8), (min(frame.shape[1] - 8, 620), 42), (0, 0, 0), -1)
    cv2.putText(frame, text, (15, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)


def _tile_frames(frames, cols: int, cell_w: int, cell_h: int):
    if np is None:
        raise RuntimeError("Falta dependencia: numpy para backend OpenCV")
    n = len(frames)
    rows = int(math.ceil(n / max(cols, 1)))
    canvas = np.zeros((rows * cell_h, cols * cell_w, 3), dtype=np.uint8)
    canvas[:] = (20, 20, 20)

    for i, fr in enumerate(frames):
        r = i // cols
        c = i % cols
        y1, y2 = r * cell_h, (r + 1) * cell_h
        x1, x2 = c * cell_w, (c + 1) * cell_w
        canvas[y1:y2, x1:x2] = fr
    return canvas


def _detect_people_hog(frame, hog, opencv_cfg: dict):
    hit_threshold = float(opencv_cfg.get("hog_hit_threshold", 0.0))
    scale = float(opencv_cfg.get("hog_scale", 1.05))
    win_stride = opencv_cfg.get("hog_win_stride", [8, 8])
    padding = opencv_cfg.get("hog_padding", [8, 8])
    min_conf = float(opencv_cfg.get("hog_min_confidence", 0.0))

    try:
        ws = (int(win_stride[0]), int(win_stride[1]))
    except Exception:
        ws = (8, 8)
    try:
        pad = (int(padding[0]), int(padding[1]))
    except Exception:
        pad = (8, 8)

    rects, weights = hog.detectMultiScale(
        frame,
        hitThreshold=hit_threshold,
        winStride=ws,
        padding=pad,
        scale=scale,
    )

    detections = []
    if len(rects) == 0:
        return detections

    for (x, y, w, h), wt in zip(rects, weights):
        conf = float(wt[0] if hasattr(wt, "__len__") else wt)
        if conf < min_conf:
            continue
        x1, y1 = int(x), int(y)
        x2, y2 = int(x + w), int(y + h)
        detections.append((x1, y1, x2, y2, conf))
    return detections


class SimpleCentroidTracker:
    def __init__(self, max_disappeared: int = 20, max_distance: float = 80.0):
        self.next_object_id = 1
        self.objects: Dict[int, Tuple[float, float]] = {}
        self.boxes: Dict[int, Tuple[int, int, int, int]] = {}
        self.scores: Dict[int, float] = {}
        self.disappeared: Dict[int, int] = {}
        self.max_disappeared = max(1, int(max_disappeared))
        self.max_distance = float(max_distance)

    def _register(self, centroid, bbox, score):
        oid = self.next_object_id
        self.next_object_id += 1
        self.objects[oid] = centroid
        self.boxes[oid] = bbox
        self.scores[oid] = float(score)
        self.disappeared[oid] = 0

    def _deregister(self, object_id: int):
        self.objects.pop(object_id, None)
        self.boxes.pop(object_id, None)
        self.scores.pop(object_id, None)
        self.disappeared.pop(object_id, None)

    def _build_result(self):
        result = {}
        for oid, centroid in self.objects.items():
            result[oid] = {
                "centroid": centroid,
                "bbox": self.boxes.get(oid),
                "score": self.scores.get(oid, 0.0),
            }
        return result

    def update(self, detections):
        if len(detections) == 0:
            for oid in list(self.disappeared.keys()):
                self.disappeared[oid] += 1
                if self.disappeared[oid] > self.max_disappeared:
                    self._deregister(oid)
            return self._build_result()

        input_centroids = []
        input_boxes = []
        input_scores = []
        for (x1, y1, x2, y2, score) in detections:
            cx = float((x1 + x2) / 2.0)
            cy = float((y1 + y2) / 2.0)
            input_centroids.append((cx, cy))
            input_boxes.append((int(x1), int(y1), int(x2), int(y2)))
            input_scores.append(float(score))

        if len(self.objects) == 0:
            for c, b, sc in zip(input_centroids, input_boxes, input_scores):
                self._register(c, b, sc)
            return self._build_result()

        object_ids = list(self.objects.keys())
        object_centroids = list(self.objects.values())

        if np is None:
            raise RuntimeError("Falta dependency numpy para tracking OpenCV")

        D = np.zeros((len(object_centroids), len(input_centroids)), dtype=np.float32)
        for i, (ox, oy) in enumerate(object_centroids):
            for j, (ix, iy) in enumerate(input_centroids):
                D[i, j] = math.hypot(ix - ox, iy - oy)

        rows = D.min(axis=1).argsort()
        cols = D.argmin(axis=1)[rows]

        used_rows = set()
        used_cols = set()

        for row, col in zip(rows, cols):
            if row in used_rows or col in used_cols:
                continue
            if D[row, col] > self.max_distance:
                continue

            oid = object_ids[row]
            self.objects[oid] = input_centroids[col]
            self.boxes[oid] = input_boxes[col]
            self.scores[oid] = input_scores[col]
            self.disappeared[oid] = 0
            used_rows.add(row)
            used_cols.add(col)

        unused_rows = set(range(D.shape[0])) - used_rows
        unused_cols = set(range(D.shape[1])) - used_cols

        for row in unused_rows:
            oid = object_ids[row]
            self.disappeared[oid] += 1
            if self.disappeared[oid] > self.max_disappeared:
                self._deregister(oid)

        for col in unused_cols:
            self._register(input_centroids[col], input_boxes[col], input_scores[col])

        return self._build_result()


def run_counter_opencv(store: ConfigStore, no_display: bool = False) -> None:
    if cv2 is None:
        raise RuntimeError("Falta dependencia: python3-opencv")
    if np is None:
        raise RuntimeError("Falta dependencia: numpy")

    cameras = store.data.get("cameras", [])
    if len(cameras) == 0:
        raise RuntimeError("No hay camaras en configuracion.")
    if len(cameras) < 4:
        print("[WARN] Tienes menos de 4 camaras configuradas. El script soporta 4+ sin problema.")

    settings = store.data["settings"]
    opencv_cfg = store.data.get("opencv", {})

    if not no_display and not os.environ.get("DISPLAY"):
        print("[WARN] DISPLAY no definido (entorno headless). Se activa --no-display automaticamente.")
        no_display = True

    detector_name = str(opencv_cfg.get("detector", "hog")).lower()
    if detector_name != "hog":
        print(f"[WARN] detector OpenCV no soportado: {detector_name}. Usando hog.")

    hog = cv2.HOGDescriptor()
    hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    max_disappeared = int(opencv_cfg.get("tracker_max_disappeared_frames", 20))
    max_distance = float(opencv_cfg.get("tracker_max_match_distance_px", 80.0))
    detect_every = max(1, int(opencv_cfg.get("detect_every_n_frames", 2)))

    src_w = int(settings.get("source_width", 1280))
    src_h = int(settings.get("source_height", 720))

    per_cam = {}
    for cam in cameras:
        cam_id = str(cam["id"])
        cap, backend_name = _open_capture_with_fallback(cam["uri"])
        if cap is None:
            print(f"[WARN] No se pudo abrir {cam.get('name', cam_id)}. Se intentara reconectar en runtime.")
        else:
            print(f"[INFO] {cam.get('name', cam_id)} abierto con backend {backend_name}.")
        per_cam[cam_id] = {
            "cap": cap,
            "backend": backend_name,
            "frame_num": 0,
            "tracker": SimpleCentroidTracker(max_disappeared=max_disappeared, max_distance=max_distance),
            "last_detections": [],
        }

    core = CounterCore(store)
    mqtt_pub = MQTTPublisher(store)
    mqtt_pub.start()

    interval_sec = int(settings.get("interval_sec", 60))
    last_tick = time.monotonic()
    win_name = "People Counter (OpenCV)"

    cols = int(math.ceil(math.sqrt(len(cameras))))

    try:
        while True:
            frames = []
            for cam in cameras:
                cam_id = str(cam["id"])
                st = per_cam[cam_id]
                cap = st["cap"]

                frame = None
                if cap is not None:
                    ok, fr = cap.read()
                    if ok and fr is not None:
                        frame = fr
                    else:
                        cap.release()
                        st["cap"] = None

                if st["cap"] is None:
                    new_cap, backend_name = _open_capture_with_fallback(cam["uri"])
                    st["cap"] = new_cap
                    st["backend"] = backend_name
                    if new_cap is not None:
                        ok, fr = new_cap.read()
                        if ok and fr is not None:
                            frame = fr

                if frame is None:
                    frame = _blank_frame(src_w, src_h, f"Sin video: {cam.get('name', cam_id)}")
                    frames.append(frame)
                    continue

                frame = cv2.resize(frame, (src_w, src_h), interpolation=cv2.INTER_AREA)
                st["frame_num"] += 1
                frame_num = int(st["frame_num"])

                if frame_num % detect_every == 0:
                    det_w = int(opencv_cfg.get("detect_resize_width", 640))
                    det_h = int(opencv_cfg.get("detect_resize_height", 360))
                    use_downscale = det_w > 0 and det_h > 0 and (frame.shape[1] != det_w or frame.shape[0] != det_h)

                    if use_downscale:
                        det_frame = cv2.resize(frame, (det_w, det_h), interpolation=cv2.INTER_LINEAR)
                        det_small = _detect_people_hog(det_frame, hog, opencv_cfg)
                        sx = float(frame.shape[1]) / float(det_w)
                        sy = float(frame.shape[0]) / float(det_h)
                        detections = [
                            (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy), conf)
                            for (x1, y1, x2, y2, conf) in det_small
                        ]
                    else:
                        detections = _detect_people_hog(frame, hog, opencv_cfg)

                    st["last_detections"] = detections
                else:
                    detections = st["last_detections"]

                tracks = st["tracker"].update(detections)
                for oid, info in tracks.items():
                    cx, cy = info["centroid"]
                    core.process_object(cam, frame_num, int(oid), (float(cx), float(cy)), None)
                    bbox = info.get("bbox")
                    if bbox is not None:
                        x1, y1, x2, y2 = bbox
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (20, 180, 255), 2)
                    cv2.putText(
                        frame,
                        f"ID:{int(oid)}",
                        (int(cx) + 4, int(cy) - 4),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (20, 180, 255),
                        2,
                        cv2.LINE_AA,
                    )

                core.prune_tracks(cam_id, frame_num)
                _draw_overlay_opencv(frame, cam, core)
                frames.append(frame)

            if not no_display:
                tiled = _tile_frames(frames, cols=cols, cell_w=src_w, cell_h=src_h)
                cv2.imshow(win_name, tiled)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q"), ord("Q")):
                    raise KeyboardInterrupt

            now = time.monotonic()
            if now - last_tick >= interval_sec:
                with store.lock:
                    messages = core.build_interval_messages()
                    for message in messages:
                        mqtt_pub.publish_or_queue(message)
                    core.reset_minute()
                    if messages:
                        store.data["state"]["last_minute_payload_utc"] = messages[0]["payload"]["time"]
                    store.save()
                last_tick = now

    except KeyboardInterrupt:
        print("Deteniendo por teclado...")
    finally:
        try:
            with store.lock:
                messages = core.build_interval_messages()
                for message in messages:
                    mqtt_pub.publish_or_queue(message)
                core.reset_minute()
                if messages:
                    store.data["state"]["last_minute_payload_utc"] = messages[0]["payload"]["time"]
                store.save()
        except Exception:
            pass

        for st in per_cam.values():
            cap = st.get("cap")
            if cap is not None:
                cap.release()
        if not no_display:
            cv2.destroyAllWindows()
        mqtt_pub.stop()


def run_counter(store: ConfigStore, no_display: bool = False) -> None:
    require_runtime_modules()

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    import pyds as pyds_mod

    global pyds
    pyds = pyds_mod

    cameras = store.data.get("cameras", [])
    if len(cameras) == 0:
        raise RuntimeError("No hay camaras en configuracion.")
    if len(cameras) < 4:
        print("[WARN] Tienes menos de 4 camaras configuradas. El script soporta 4+ sin problema.")

    settings = store.data["settings"]
    ds_cfg = store.data["deepstream"]
    pgie_config_path = ds_cfg.get("pgie_config_path")
    if not pgie_config_path or not Path(pgie_config_path).exists():
        pgie_candidates = [
            pgie_config_path or "",
            "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_infer_primary.txt",
            "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_infer_primary.txt",
        ]
        resolved = first_existing_path(pgie_candidates)
        if resolved is None:
            raise RuntimeError(f"pgie_config_path no existe: {pgie_config_path}")
        print(f"[WARN] pgie_config_path ajustado automaticamente a: {resolved}")
        pgie_config_path = resolved

    engine_path = _read_pgie_key(pgie_config_path, "model-engine-file")
    if engine_path and not Path(engine_path).exists():
        print(f"[INFO] TensorRT engine no existe aun: {engine_path}")
        print("[INFO] Primer arranque: DeepStream puede tardar 1-5+ minutos en construirlo.")
        engine_dir = str(Path(engine_path).parent)
        if not os.access(engine_dir, os.W_OK):
            print(f"[WARN] Sin permisos de escritura en {engine_dir}; el engine podria regenerarse en cada inicio.")

    if not no_display and not os.environ.get("DISPLAY"):
        print("[WARN] DISPLAY no definido (entorno headless). Se activa --no-display automaticamente.")
        no_display = True

    core = CounterCore(store)
    mqtt_pub = MQTTPublisher(store)
    mqtt_pub.start()

    Gst.init(None)
    pipeline = Gst.Pipeline.new("people-counter-pipeline")
    if pipeline is None:
        raise RuntimeError("No se pudo crear pipeline")

    streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "tiler")
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "nvvidconv")
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    queue1 = Gst.ElementFactory.make("queue", "q1")
    queue2 = Gst.ElementFactory.make("queue", "q2")
    queue3 = Gst.ElementFactory.make("queue", "q3")
    queue4 = Gst.ElementFactory.make("queue", "q4")
    queue5 = Gst.ElementFactory.make("queue", "q5")

    if not all([streammux, pgie, tracker, tiler, nvvidconv, nvosd, queue1, queue2, queue3, queue4, queue5]):
        raise RuntimeError("No se pudieron crear elementos de pipeline DeepStream")

    pipeline.add(streammux)

    for i, cam in enumerate(cameras):
        source_bin = create_source_bin(i, cam["uri"])
        pipeline.add(source_bin)
        if hasattr(streammux, "request_pad_simple"):
            sinkpad = streammux.request_pad_simple(f"sink_{i}")
        else:
            sinkpad = streammux.get_request_pad(f"sink_{i}")
        if sinkpad is None:
            raise RuntimeError(f"No se pudo obtener sink pad sink_{i} de nvstreammux")
        srcpad = source_bin.get_static_pad("src")
        if srcpad is None:
            raise RuntimeError("No se pudo obtener src pad de source_bin")
        if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"No se pudo linkear camara {cam['id']} a streammux")

    pipeline.add(queue1)
    pipeline.add(pgie)
    pipeline.add(queue2)
    pipeline.add(tracker)
    pipeline.add(queue3)
    pipeline.add(tiler)
    pipeline.add(queue4)
    pipeline.add(nvvidconv)
    pipeline.add(queue5)
    pipeline.add(nvosd)

    if no_display:
        sink = Gst.ElementFactory.make("fakesink", "fakesink")
        pipeline.add(sink)
        sink.set_property("sync", False)
    else:
        if platform.machine() == "aarch64":
            transform = Gst.ElementFactory.make("nvegltransform", "egl-transform")
            sink = Gst.ElementFactory.make("nveglglessink", "egl-sink")
            if not transform or not sink:
                raise RuntimeError("No se pudo crear nvegltransform/nveglglessink")
            sink.set_property("sync", False)
            pipeline.add(transform)
            pipeline.add(sink)
        else:
            sink = Gst.ElementFactory.make("autovideosink", "video-sink")
            pipeline.add(sink)

    live_source = any(str(c.get("uri", "")).startswith("rtsp://") for c in cameras)
    streammux.set_property("batch-size", len(cameras))
    streammux.set_property("width", int(settings.get("source_width", 1280)))
    streammux.set_property("height", int(settings.get("source_height", 720)))
    streammux.set_property("batched-push-timeout", int(settings.get("streammux_batch_timeout_us", 40000)))
    streammux.set_property("live-source", 1 if live_source else 0)

    pgie.set_property("config-file-path", pgie_config_path)
    if pgie.get_property("batch-size") != len(cameras):
        pgie.set_property("batch-size", len(cameras))

    tracker_cfg = dict(ds_cfg.get("tracker", {}))
    ll_lib_path = str(tracker_cfg.get("ll-lib-file", "")).strip()
    if not ll_lib_path or not Path(ll_lib_path).exists():
        lib_candidates = [
            ll_lib_path,
            "/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_nvmultiobjecttracker.so",
            "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so",
        ]
        resolved = first_existing_path(lib_candidates)
        if resolved is None:
            raise RuntimeError(f"tracker ll-lib-file no existe: {ll_lib_path}")
        print(f"[WARN] tracker ll-lib-file ajustado automaticamente a: {resolved}")
        tracker_cfg["ll-lib-file"] = resolved

    ll_cfg_path = str(tracker_cfg.get("ll-config-file", "")).strip()
    if not ll_cfg_path or not Path(ll_cfg_path).exists():
        cfg_candidates = [
            ll_cfg_path,
            "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml",
            "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml",
        ]
        resolved = first_existing_path(cfg_candidates)
        if resolved is None:
            raise RuntimeError(f"tracker ll-config-file no existe: {ll_cfg_path}")
        print(f"[WARN] tracker ll-config-file ajustado automaticamente a: {resolved}")
        tracker_cfg["ll-config-file"] = resolved

    if no_display:
        active_tracker_cfg = str(tracker_cfg.get("ll-config-file", ""))
        if "nvdcf" in active_tracker_cfg.lower():
            iou_candidate = str(Path(active_tracker_cfg).with_name("config_tracker_IOU.yml"))
            if Path(iou_candidate).exists():
                print(f"[WARN] Modo headless: usando tracker IOU para evitar fallos EGL de NvDCF: {iou_candidate}")
                tracker_cfg["ll-config-file"] = iou_candidate

    for key, value in tracker_cfg.items():
        _set_if_prop_exists(tracker, key, value)

    rows = int(math.sqrt(len(cameras))) or 1
    cols = int(math.ceil(len(cameras) / rows))
    tiler.set_property("rows", rows)
    tiler.set_property("columns", cols)
    tiler.set_property("width", int(settings.get("source_width", 1280) * cols))
    tiler.set_property("height", int(settings.get("source_height", 720) * rows))

    _link_elements_or_raise(streammux, queue1, pgie, queue2, tracker, queue3, tiler, queue4, nvvidconv, queue5, nvosd)

    if no_display:
        if not nvosd.link(sink):
            raise RuntimeError("Error linkeando sink")
    else:
        if platform.machine() == "aarch64":
            if not nvosd.link(transform) or not transform.link(sink):
                raise RuntimeError("Error linkeando output con EGL")
        else:
            if not nvosd.link(sink):
                raise RuntimeError("Error linkeando output")

    sink_pad = tracker.get_static_pad("src")
    if sink_pad is None:
        raise RuntimeError("No se pudo obtener tracker src pad")

    person_class_id = int(settings.get("person_class_id", 0))
    min_conf = float(settings.get("min_detector_confidence", 0.25))

    def add_overlay(frame_meta, batch_meta, cam):
        cam_id = str(cam["id"])
        t = core.get_totals(cam_id)
        inside = int(t["in"]) - int(t["out"])
        text = f"{cam.get('name', cam_id)} | In:{int(t['in'])} Out:{int(t['out'])} Inside:{inside}"
        line = cam.get("line", {})
        p1 = line.get("p1", [100, 100])
        p2 = line.get("p2", [400, 100])

        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        display_meta.num_lines = 1

        txt_params = display_meta.text_params[0]
        txt_params.display_text = text
        txt_params.x_offset = 20
        txt_params.y_offset = 30
        txt_params.font_params.font_name = "Serif"
        txt_params.font_params.font_size = 15
        txt_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
        txt_params.set_bg_clr = 1
        txt_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.65)

        line_params = display_meta.line_params[0]
        line_params.x1 = int(p1[0])
        line_params.y1 = int(p1[1])
        line_params.x2 = int(p2[0])
        line_params.y2 = int(p2[1])
        line_params.line_width = 3
        line_params.line_color.set(0.0, 1.0, 0.0, 1.0)

        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

    def probe_func(pad, info, user_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK

        with store.lock:
            l_frame = batch_meta.frame_meta_list
            while l_frame is not None:
                try:
                    frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
                except StopIteration:
                    break

                src_idx = int(frame_meta.pad_index)
                if src_idx < 0 or src_idx >= len(cameras):
                    try:
                        l_frame = l_frame.next
                    except StopIteration:
                        break
                    continue

                cam = cameras[src_idx]
                cam_id = str(cam["id"])
                l_obj = frame_meta.obj_meta_list

                while l_obj is not None:
                    try:
                        obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    except StopIteration:
                        break

                    if core._is_person_meta(obj_meta, person_class_id) and float(obj_meta.confidence) >= min_conf:
                        oid = int(obj_meta.object_id)
                        if oid >= 0:
                            rect = obj_meta.rect_params
                            cx = float(rect.left + rect.width * 0.5)
                            cy = float(rect.top + rect.height * 0.5)
                            core.process_object(cam, int(frame_meta.frame_num), oid, (cx, cy), obj_meta)
                    try:
                        l_obj = l_obj.next
                    except StopIteration:
                        break

                core.prune_tracks(cam_id, int(frame_meta.frame_num))
                add_overlay(frame_meta, batch_meta, cam)
                try:
                    l_frame = l_frame.next
                except StopIteration:
                    break
        return Gst.PadProbeReturn.OK

    sink_pad.add_probe(Gst.PadProbeType.BUFFER, probe_func, None)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    interval_sec = int(settings.get("interval_sec", 60))

    def minute_tick():
        with store.lock:
            messages = core.build_interval_messages()
            for message in messages:
                mqtt_pub.publish_or_queue(message)
            core.reset_minute()
            if messages:
                store.data["state"]["last_minute_payload_utc"] = messages[0]["payload"]["time"]
            store.save()
        return True

    GLib.timeout_add_seconds(interval_sec, minute_tick)

    print(f"Iniciando pipeline con {len(cameras)} camaras...")
    mqtt_cfg = store.data.get("settings", {}).get("mqtt", {})
    active_topic = mqtt_cfg.get("topic_template") or "location/{location_id}/device/{device_id}/event/up"
    print(f"Envio MQTT cada {interval_sec}s a topic/template: {active_topic}")
    pipeline.set_state(Gst.State.PLAYING)

    try:
        loop.run()
    except KeyboardInterrupt:
        print("Deteniendo por teclado...")
    finally:
        try:
            with store.lock:
                messages = core.build_interval_messages()
                for message in messages:
                    mqtt_pub.publish_or_queue(message)
                core.reset_minute()
                if messages:
                    store.data["state"]["last_minute_payload_utc"] = messages[0]["payload"]["time"]
                store.save()
        except Exception:
            pass
        pipeline.set_state(Gst.State.NULL)
        mqtt_pub.stop()


def main():
    parser = argparse.ArgumentParser(description="Contador de personas (DeepStream/OpenCV + MQTT) para Jetson.")
    parser.add_argument("--config", default="config.json", help="Ruta del JSON de configuracion/estado.")
    parser.add_argument("--init", action="store_true", help="Crea plantilla de configuracion y termina.")
    parser.add_argument("--calibrate", action="store_true", help="Modo calibracion de lineas por camara.")
    parser.add_argument("--backend", choices=["deepstream", "opencv"], default=None, help="Backend de procesamiento (override sobre config).")
    parser.add_argument("--no-display", action="store_true", help="Ejecuta sin mostrar video.")
    args = parser.parse_args()

    cfg_path = Path(args.config).resolve()
    store = ConfigStore(cfg_path)

    if args.init:
        if cfg_path.exists():
            print(f"Ya existe: {cfg_path}")
            print("Si quieres regenerarlo, borralo manualmente y vuelve a ejecutar --init.")
            return 0
        store.create_template()
        print(f"Plantilla creada en: {cfg_path}")
        print("Edita RTSP/MQTT y luego ejecuta --calibrate.")
        return 0

    if not cfg_path.exists():
        store.create_template()
        print(f"Se creo plantilla por defecto en: {cfg_path}")
        print("Edita RTSP/MQTT y vuelve a ejecutar.")
        return 1

    store.load()

    if args.calibrate:
        calibrate_lines(store)
        return 0

    backend = str(args.backend or store.data.get("runtime", {}).get("backend", "deepstream")).lower()
    if backend not in ("deepstream", "opencv"):
        print(f"[WARN] backend invalido en config: {backend}. Se usara deepstream.")
        backend = "deepstream"

    print(f"Backend seleccionado: {backend}")
    if backend == "opencv":
        run_counter_opencv(store, no_display=args.no_display)
    else:
        run_counter(store, no_display=args.no_display)
    return 0


if __name__ == "__main__":
    sys.exit(main())
