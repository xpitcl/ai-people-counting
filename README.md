# People Counter Jetson (DeepStream u OpenCV + MQTT)

Script para Jetson Orin Nano que cuenta personas por cruce de línea en múltiples cámaras RTSP y publica métricas por MQTT cada `interval_sec` (ej: 1, 2 o 60 segundos).

Archivo principal:
- `people_counter_jetson.py`

Archivo de configuración/estado:
- `config.json`

## Requisitos

- Jetson Orin Nano con JetPack + DeepStream instalado.
- Python 3.
- Dependencias Python:
  - `paho-mqtt`
  - `opencv-python` o `python3-opencv`
  - `gi` (PyGObject, normalmente viene en Jetson)
  - `pyds` (bindings de DeepStream, incluidos con DeepStream)

Instalación típica de dependencias Python:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install paho-mqtt
sudo apt-get update
sudo apt-get install -y python3-opencv python3-gi
```

## Configuración rápida

1. Edita `config.json`:
- `settings.mqtt.host`, `port`, credenciales.
- `settings.mqtt.topic_template`: template por cámara (por defecto `location/{location_id}/device/{device_id}/event/up`).
- `cameras[].uri` con tus RTSP reales.
- `cameras[].device_id` (o `deviceID`, `uid`, `ud`, `id`) define el `deviceID` publicado por MQTT.
- `cameras[].location_id` y `cameras[].line.id` para identificar ubicación y línea.
- `runtime.backend`: `deepstream` u `opencv`.
- (Opcional) `deepstream.pgie_config_path` y `deepstream.tracker.*` si usas DeepStream.
- (Opcional) `opencv.*` para ajustar detector/tracker en backend OpenCV.

2. Calibra líneas de conteo:

```bash
python3 people_counter_jetson.py --calibrate
```

Controles de calibración:
- Click izquierdo: definir punto 1 y punto 2 de la línea.
- `W/A/S/D`: dirección de entrada (`in_direction`).
- Click derecho (con línea ya definida): dirección personalizada hacia donde clickeas.
- `R`: reset.
- `Enter`: guardar.
- `Esc`: cancelar.

## Ejecución

Con visualización (mosaico de cámaras):

```bash
python3 people_counter_jetson.py
```

Forzar backend por CLI:

```bash
python3 people_counter_jetson.py --backend deepstream
python3 people_counter_jetson.py --backend opencv
```

Sin visualización:

```bash
python3 people_counter_jetson.py --no-display
```

Nota: el script solo fuerza `--no-display` si no detecta `DISPLAY` (entorno headless real).


## Backends

- `deepstream`:
  - Mejor rendimiento y uso de aceleración NVIDIA.
  - Requiere entorno DeepStream/GStreamer correctamente configurado.
- `opencv`:
  - Fallback útil cuando hay problemas de EGL/X11 con DeepStream.
  - Usa detector HOG + tracker centroid (menos preciso/eficiente que DeepStream en muchos escenarios).

## Qué publica por MQTT

Cada `interval_sec` publica **un mensaje por cámara** con formato IoT:

Topic (por cámara):
```text
location/{location_id}/device/{device_id}/event/up
```

Payload ejemplo:
```json
{
  "schema_version": 1,
  "message_type": "metric",
  "msg_id": "e5b9b11d-3c5a-4ac8-93a9-4e4f6ab2f7e2",
  "time": "2026-04-23T15:10:00Z",
  "window_start": "2026-04-23T15:09:58Z",
  "window_end": "2026-04-23T15:10:00Z",
  "protocol": "Vision",
  "deviceID": "cam_1",
  "locationID": "site_a",
  "device_info": {
    "friendly_name": "Camara 1",
    "manufacturer_name": "NVIDIA",
    "device_model": "Jetson Orin Nano",
    "device_type": "CameraEdge",
    "host": "jetson-orin-nano"
  },
  "object": {
    "line_id": "cam_1_line_1",
    "people_in_interval": 2,
    "people_out_interval": 1,
    "people_in_total": 24,
    "people_out_total": 17,
    "people_occupancy": 7,
    "interval_sec": 2
  }
}
```

`line_id` es el campo estático por línea (hoy una línea por cámara), preparado para múltiples líneas futuras.

Si el broker está caído:
- guarda payloads pendientes en `state.pending_mqtt` dentro de `config.json`.
- al reconectar, reintenta y vacía la cola.

## Notas operativas

- `people_occupancy` se calcula como `people_in_total - people_out_total`.
- Si reinicias el script, conserva estado acumulado y cola MQTT en el JSON.
- Para 4+ cámaras, ajusta resolución y `streammux_batch_timeout_us` según rendimiento.

## Comandos útiles

Crear plantilla de configuración (si no existe):

```bash
python3 people_counter_jetson.py --init
```

Usar otro archivo de configuración:

```bash
python3 people_counter_jetson.py --config /ruta/mi_config.json
```
