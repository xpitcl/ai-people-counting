#!/usr/bin/env python3
"""
People counter for Jetson Orin Nano using NVIDIA DeepStream + MQTT.

Features:
- Multi-RTSP ingest (4+ cameras supported).
- Interactive line calibration per camera (saved in JSON config).
- In/Out/Inside counting per camera based on line crossing direction.
- Tiled display with per-camera counters.
- MQTT publish every minute, with persistent offline queue/retry.
- Optional per-person attributes from DeepStream classifier metadata.

Usage:
1) Create template config:
   python3 people_counter_jetson.py --init

2) Edit RTSP URIs and MQTT server in config.json.

3) Calibrate lines:
   python3 people_counter_jetson.py --calibrate

4) Run counter:
   python3 people_counter_jetson.py
"""

import argparse
import copy
import datetime as dt
import json
import math
import os
import platform
import socket
import sys
import threading
import time
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


def default_config() -> dict:
    return {
        "settings": {
            "interval_sec": 60,
            "person_class_id": 0,
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
                "topic": "people_counter/site_a",
                "client_id": "jetson-orin-nano-counter",
                "username": "",
                "password": "",
                "qos": 1,
                "retain": False,
                "keepalive": 30,
            },
        },
        "deepstream": {
            "pgie_config_path": "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_infer_primary_peoplenet.txt",
            "tracker": {
                "tracker-width": 640,
                "tracker-height": 384,
                "gpu-id": 0,
                "ll-lib-file": "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so",
                "ll-config-file": "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml",
                "enable-batch-process": 1,
                "enable-past-frame": 0,
            },
        },
        "cameras": [
            {
                "id": "cam_1",
                "name": "Camara 1",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"p1": [200, 360], "p2": [1080, 360], "in_direction": [0, -1]},
            },
            {
                "id": "cam_2",
                "name": "Camara 2",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"p1": [640, 100], "p2": [640, 620], "in_direction": [1, 0]},
            },
            {
                "id": "cam_3",
                "name": "Camara 3",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"p1": [220, 130], "p2": [1000, 600], "in_direction": [0, 1]},
            },
            {
                "id": "cam_4",
                "name": "Camara 4",
                "uri": "rtsp://USER:PASS@IP:554/stream1",
                "line": {"p1": [300, 100], "p2": [300, 700], "in_direction": [-1, 0]},
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
        self.data.setdefault("deepstream", {})
        self.data.setdefault("cameras", [])
        self.data.setdefault("state", {})
        self.data["state"].setdefault("totals", {})
        self.data["state"].setdefault("pending_mqtt", [])
        self.data["state"].setdefault("last_minute_payload_utc", None)

        for idx, cam in enumerate(self.data["cameras"]):
            cam.setdefault("id", f"cam_{idx+1}")
            cam.setdefault("name", cam["id"])
            cam.setdefault("line", {})
            cam["line"].setdefault("p1", [200, 200])
            cam["line"].setdefault("p2", [1000, 200])
            cam["line"].setdefault("in_direction", [0, -1])
            cam_id = str(cam["id"])
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

    def _publish(self, payload: dict) -> bool:
        cfg = self.store.data["settings"]["mqtt"]
        topic = cfg["topic"]
        qos = int(cfg.get("qos", 1))
        retain = bool(cfg.get("retain", False))
        body = json.dumps(payload, ensure_ascii=True)
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

    def enqueue_pending(self, payload: dict) -> None:
        with self.store.lock:
            self.store.data["state"]["pending_mqtt"].append(payload)
            self.store.save()

    def flush_pending(self) -> None:
        with self.store.lock:
            pending = self.store.data["state"]["pending_mqtt"]
            if not pending:
                return
            remaining = []
            for payload in pending:
                if not self._publish(payload):
                    remaining.append(payload)
            self.store.data["state"]["pending_mqtt"] = remaining
            self.store.save()

    def publish_or_queue(self, payload: dict) -> None:
        self.flush_pending()
        if not self._publish(payload):
            self.enqueue_pending(payload)


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

    def process_object(
        self,
        cam: dict,
        frame_num: int,
        object_id: int,
        centroid: Tuple[float, float],
        obj_meta,
    ) -> None:
        settings = self.store.data["settings"]
        cam_id = str(cam["id"])
        p1 = tuple(cam["line"]["p1"])
        p2 = tuple(cam["line"]["p2"])
        in_vec = normalize_vec(cam["line"]["in_direction"])
        deadband = float(settings.get("line_deadband_px", 3.0))
        min_gap = int(settings.get("min_crossing_gap_frames", 12))

        side = self._line_side(p1, p2, centroid, deadband)
        state = self.track_state[cam_id].setdefault(
            object_id, {"prev_pt": None, "prev_side": 0, "last_cross_frame": -10_000, "last_seen": frame_num}
        )
        prev_pt = state["prev_pt"]
        prev_side = int(state["prev_side"])

        if prev_pt is not None and prev_side != 0 and side != 0 and prev_side * side < 0:
            if frame_num - int(state["last_cross_frame"]) >= min_gap:
                mv = (centroid[0] - prev_pt[0], centroid[1] - prev_pt[1])
                dot = mv[0] * in_vec[0] + mv[1] * in_vec[1]
                if abs(dot) > 1e-6:
                    direction = "in" if dot > 0 else "out"
                    self.get_totals(cam_id)[direction] += 1
                    self.minute_counts[cam_id][direction] += 1
                    state["last_cross_frame"] = frame_num
                    self.minute_events.append(
                        {
                            "ts_utc": utc_now_iso(),
                            "camera_id": cam_id,
                            "camera_name": cam.get("name", cam_id),
                            "track_id": int(object_id),
                            "direction": direction,
                            "attrs": self._extract_attrs(obj_meta),
                        }
                    )

        state["prev_pt"] = centroid
        state["prev_side"] = side
        state["last_seen"] = frame_num

    def prune_tracks(self, cam_id: str, frame_num: int) -> None:
        max_idle = int(self.store.data["settings"].get("track_max_idle_frames", 120))
        tmap = self.track_state[cam_id]
        stale = [tid for tid, st in tmap.items() if frame_num - int(st.get("last_seen", frame_num)) > max_idle]
        for tid in stale:
            del tmap[tid]

    def build_minute_payload(self) -> dict:
        payload = {
            "ts_utc": utc_now_iso(),
            "device": self.hostname,
            "interval_sec": int(self.store.data["settings"].get("interval_sec", 60)),
            "cameras": [],
            "events": copy.deepcopy(self.minute_events),
        }
        for cam in self.store.data["cameras"]:
            cam_id = str(cam["id"])
            totals = self.get_totals(cam_id)
            minute = self.minute_counts[cam_id]
            payload["cameras"].append(
                {
                    "id": cam_id,
                    "name": cam.get("name", cam_id),
                    "in": int(minute["in"]),
                    "out": int(minute["out"]),
                    "inside": int(totals["in"]) - int(totals["out"]),
                    "total_in": int(totals["in"]),
                    "total_out": int(totals["out"]),
                }
            )
        return payload

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
        import pyds  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "No se pudo importar GStreamer/DeepStream Python bindings (gi + pyds). "
            "Asegura tener DeepStream instalado en Jetson."
        ) from e


def draw_line_ui(frame, cam_label: str, initial_line: dict) -> Optional[dict]:
    points = []
    if initial_line and "p1" in initial_line and "p2" in initial_line:
        points = [tuple(initial_line["p1"]), tuple(initial_line["p2"])]
    in_dir = tuple(initial_line.get("in_direction", [0, -1])) if initial_line else (0, -1)
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
                return {"p1": [points[0][0], points[0][1]], "p2": [points[1][0], points[1][1]], "in_direction": [in_dir[0], in_dir[1]]}


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

    for cam in cams:
        uri = cam.get("uri", "")
        if not uri or "rtsp://" not in uri:
            print(f"[WARN] Saltando {cam.get('name', cam.get('id'))}: URI RTSP invalida.")
            continue
        cap = cv2.VideoCapture(uri)
        if not cap.isOpened():
            print(f"[WARN] No se pudo abrir {cam.get('name', cam.get('id'))}: {uri}")
            continue

        frame = None
        for _ in range(60):
            ok, fr = cap.read()
            if ok and fr is not None:
                frame = fr
                break
            time.sleep(0.05)
        cap.release()

        if frame is None:
            print(f"[WARN] Sin frame para {cam.get('name', cam.get('id'))}.")
            continue

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
        raise RuntimeError(f"pgie_config_path no existe: {pgie_config_path}")

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

    tracker_cfg = ds_cfg.get("tracker", {})
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

                    if int(obj_meta.class_id) == person_class_id and float(obj_meta.confidence) >= min_conf:
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
            payload = core.build_minute_payload()
            mqtt_pub.publish_or_queue(payload)
            core.reset_minute()
            store.data["state"]["last_minute_payload_utc"] = payload["ts_utc"]
            store.save()
        return True

    GLib.timeout_add_seconds(interval_sec, minute_tick)

    print(f"Iniciando pipeline con {len(cameras)} camaras...")
    print(f"Envio MQTT cada {interval_sec}s a topic: {store.data['settings']['mqtt']['topic']}")
    pipeline.set_state(Gst.State.PLAYING)

    try:
        loop.run()
    except KeyboardInterrupt:
        print("Deteniendo por teclado...")
    finally:
        try:
            with store.lock:
                payload = core.build_minute_payload()
                mqtt_pub.publish_or_queue(payload)
                core.reset_minute()
                store.save()
        except Exception:
            pass
        pipeline.set_state(Gst.State.NULL)
        mqtt_pub.stop()


def main():
    parser = argparse.ArgumentParser(description="Contador de personas (DeepStream + MQTT) para Jetson.")
    parser.add_argument("--config", default="config.json", help="Ruta del JSON de configuracion/estado.")
    parser.add_argument("--init", action="store_true", help="Crea plantilla de configuracion y termina.")
    parser.add_argument("--calibrate", action="store_true", help="Modo calibracion de lineas por camara.")
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

    run_counter(store, no_display=args.no_display)
    return 0


if __name__ == "__main__":
    sys.exit(main())
