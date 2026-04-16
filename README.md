# People Counter Jetson (DeepStream u OpenCV + MQTT)

Script para Jetson Orin Nano que cuenta personas por cruce de línea en múltiples cámaras RTSP y publica métricas por MQTT cada 60 segundos.

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
- `settings.mqtt.host`, `port`, `topic`, credenciales.
- `cameras[].uri` con tus RTSP reales.
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

Cada `interval_sec` (por defecto 60s) publica:
- `in`, `out`, `inside` por cámara.
- totales acumulados (`total_in`, `total_out`).
- lista de eventos de cruce del minuto.

Si el broker está caído:
- guarda payloads pendientes en `state.pending_mqtt` dentro de `config.json`.
- al reconectar, reintenta y vacía la cola.

## Notas operativas

- `Inside` se calcula como `In - Out`.
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
