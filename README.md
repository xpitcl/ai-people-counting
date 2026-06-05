# People Counter Jetson (DeepStream u OpenCV + MQTT)

Script para Jetson Orin Nano que cuenta personas por cruce de línea en múltiples cámaras RTSP y publica métricas por MQTT cada `interval_sec` (ej: 1, 2 o 60 segundos).

Archivo principal:
- `people_counter_jetson.py`

Archivo de configuración/estado:
- `/data/config.json` en Docker/Coolify, o `config.json` en ejecución local.

Archivo de plantilla seguro para versionar:
- `config_template.json`

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

1. Descarga PeopleNet en la Jetson:

```bash
./scripts/setup_peoplenet.sh
```

El script descarga el modelo en `~/.cache/ai-people-counting/models/peoplenet` y prepara `~/.cache/ai-people-counting/engines` para el engine TensorRT. El primer arranque con DeepStream puede tardar varios minutos porque TensorRT compila el engine; los siguientes arranques deben reutilizarlo.

2. Edita `config.json`:
- `settings.mqtt.host`, `port`, credenciales.
- `settings.mqtt.topic_template`: template por cámara (por defecto `location/{location_id}/device/{device_id}/event/up`).
- `settings.person_class_id`: por defecto `0` para PeopleNet (`person`, `bag`, `face`).
- `cameras[].uri` con tus RTSP reales.
- `cameras[].device_id` (o `deviceID`, `uid`, `ud`, `id`) define el `deviceID` publicado por MQTT.
- `cameras[].location_id` y `cameras[].line.id` para identificar ubicación y línea.
- `runtime.backend`: `deepstream` u `opencv`.
- (Opcional) `deepstream.pgie_config_path` y `deepstream.tracker.*` si usas DeepStream.
- (Opcional) `opencv.*` para ajustar detector/tracker en backend OpenCV.

3. Calibra líneas de conteo:

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

Capturar logs largos:

```bash
python3 people_counter_jetson.py 2>&1 | tee run.log
```

## Despliegue con Coolify

El repo incluye `Dockerfile` y `docker-compose.coolify.yml` para que Coolify reconstruya y redepliegue en cada push. Este despliegue está pensado para correr Coolify en la Jetson Orin Nano o en un host Jetson con NVIDIA Container Runtime instalado.

Requisitos del host:
- JetPack compatible con DeepStream 7.1.
- Docker + NVIDIA Container Runtime funcionando.
- Acceso del contenedor a GPU con `runtime: nvidia`.
- Coolify configurado para desplegar desde este repositorio usando Docker Compose.

En Coolify:
1. Crea un nuevo recurso desde el repositorio.
2. Selecciona Docker Compose.
3. Usa `docker-compose.coolify.yml` como compose file.
4. No expongas puerto HTTP; este servicio es un worker que consume RTSP y publica MQTT.
5. Configura las variables de entorno.

Variables minimas:

```env
MQTT_HOST=tu_broker
MQTT_PORT=1883
MQTT_USERNAME=tu_usuario
MQTT_PASSWORD=tu_password
MQTT_CLIENT_ID=jetson-orin-nano-counter
CAMERAS_JSON=[{"id":"cam_1","name":"Camara 1","location_id":"site_a","uri":"rtsp://usuario:password@ip:554/Streaming/channels/101","line":{"id":"cam_1_line_1","p1":[636,62],"p2":[623,629],"in_direction":[-189,-3]}}]
```

Variables utiles:

```env
INTERVAL_SEC=60
BACKEND=deepstream
NO_DISPLAY=1
SETUP_PEOPLENET=1
CONFIG_OVERWRITE=0
CONFIG_PRESERVE_STATE=1
MQTT_QOS=1
MQTT_RETAIN=0
PERSON_CLASS_ID=0
MIN_DETECTOR_CONFIDENCE=0.25
PROCESSED_RTSP_ENABLED=0
PROCESSED_RTSP_URL=rtsp://127.0.0.1:18554/people-counter
PROCESSED_RTSP_WIDTH=640
PROCESSED_RTSP_HEIGHT=360
PROCESSED_RTSP_FPS=5
PROCESSED_RTSP_BITRATE=1000000
```

Si `PROCESSED_RTSP_ENABLED=1`, el servicio publica un RTSP H.264 con el mosaico procesado después de `nvosd`. Si hay 4 cámaras, verás una grilla 2x2 con overlays; si hay una sola cámara, verás esa cámara completa. La URL externa normalmente será:

```text
rtsp://IP_DE_LA_JETSON:18554/people-counter
```

El pipeline intenta usar `nvv4l2h264enc`; si no está disponible, cae a `x264enc`, lo que puede aumentar carga de CPU.

Configura siempre las camaras con `CAMERAS_JSON`:

```json
[
  {
    "id": "cam_1",
    "name": "Camara 1",
    "location_id": "site_a",
    "uri": "rtsp://usuario:password@ip:554/Streaming/channels/101",
    "line": {
      "id": "cam_1_line_1",
      "p1": [636, 62],
      "p2": [623, 629],
      "in_direction": [-189, -3]
    }
  }
]
```

Tambien puedes pasar un `CONFIG_JSON` completo. Por defecto el entrypoint conserva `state` desde `/data/config.json` para no perder totales ni cola MQTT en cada redeploy; cambia `CONFIG_PRESERVE_STATE=0` si quieres reemplazarlo todo.

Persistencia:
- `people-counter-data` guarda `/data/config.json`, totales y cola MQTT.
- `people-counter-cache` guarda PeopleNet y engines TensorRT.

Primer despliegue:
- `SETUP_PEOPLENET=1` descarga PeopleNet si falta.
- El primer arranque compila TensorRT y puede tardar varios minutos.
- Los siguientes redeploys reutilizan el volumen de caché.

Logs en Coolify:
- Revisa que aparezcan `PGIE config activo`, `TensorRT engine activo`, `Tracker config activo` y mensajes `[MQTT][SENT]`.
- Si falla `import pyds`, la imagen base no trae bindings Python de DeepStream; instala/builda `pyds` para DeepStream 7.1 dentro de la imagen o usa una imagen base que ya lo incluya.


## Backends

- `deepstream`:
  - Mejor rendimiento y uso de aceleración NVIDIA.
  - Requiere entorno DeepStream/GStreamer correctamente configurado.
  - Usa PeopleNet por defecto como detector primario de peatones.
  - Usa NvDCF por defecto también en modo headless.
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
- El conteo DeepStream usa metadata después de `nvtracker`; no descarta un track por baja confianza del detector si el `object_id` sigue siendo válido.

## Comandos útiles

Crear plantilla de configuración (si no existe):

```bash
python3 people_counter_jetson.py --init
```

Usar otro archivo de configuración:

```bash
python3 people_counter_jetson.py --config /ruta/mi_config.json
```

Ejecutar como servicio `systemd` (arranque automático):

```bash
sudo tee /etc/systemd/system/people-counter.service > /dev/null <<'EOF'
[Unit]
Description=People Counter (Jetson DeepStream)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=xpit
Group=xpit
WorkingDirectory=/home/xpit/Desktop/ai-people-counting
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /home/xpit/Desktop/ai-people-counting/people_counter_jetson.py --config /home/xpit/Desktop/ai-people-counting/config.json --no-display --backend deepstream
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Comandos útiles del servicio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now people-counter.service
sudo systemctl status people-counter.service --no-pager
sudo journalctl -u people-counter.service -f
sudo systemctl restart people-counter.service
sudo systemctl stop people-counter.service
sudo systemctl disable people-counter.service
```

Recomendación: ejecuta el servicio siempre con el mismo usuario (`User=xpit`) para reutilizar el caché de TensorRT y evitar recompilar engines.




## RTSP local para pruebas con video de YouTube Live

### 1. Levantar MediaMTX en puerto 18554
``` bash
docker run --rm -it --network host \
  -e MTX_RTSPADDRESS=":18554" \
  bluenviron/mediamtx
```

### 2. Obtener URL directa del live de YouTube
``` bash
STREAM_URL=$(yt-dlp -f "best[protocol^=m3u8]/best" -g "https://www.youtube.com/watch?v=M3EYAY2MftI" | head -n 1)
echo "$STREAM_URL"
```

### 3. Publicar el video como RTSP local
``` bash
ffmpeg -re -i "$STREAM_URL" \
  -an \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -f rtsp -rtsp_transport tcp \
  rtsp://127.0.0.1:18554/tienda
```

### 4. Consumir el stream
```
rtsp://127.0.0.1:18554/tienda
```
