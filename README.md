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
  - `pyds` (bindings oficiales compatibles con DeepStream 7.1)

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

El script descarga la versión ONNX podada de PeopleNet y su calibración en `~/.cache/ai-people-counting/models/peoplenet`, y prepara `~/.cache/ai-people-counting/engines` para el engine TensorRT INT8. El workspace de compilación está limitado a 256 MB para evitar agotar la memoria de la Jetson. El primer arranque puede tardar varios minutos; los siguientes deben reutilizar el engine.

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

Jetson Orin Nano no incorpora NVENC. Por eso la salida usa la ruta de software recomendada por NVIDIA: `nvvideoconvert -> I420 en memoria CPU -> x264enc`. En otros modelos Jetson con encoder disponible se prioriza `nvv4l2h264enc`.

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

### Calibrar la linea desde una Jetson con pantalla

Cuando el contador esta desplegado con Coolify, ejecuta la calibracion desde una terminal abierta en el escritorio grafico de la Jetson. No la ejecutes desde una sesion SSH sin `DISPLAY`.

### Calibrar sin OpenCV ni X11

Este flujo es el recomendado para despliegues con Coolify. No requiere entrar al contenedor ni abrir ventanas desde Docker.

Captura un frame desde la Jetson usando la misma fuente RTSP que procesa el contador:

```bash
ffmpeg -y -rtsp_transport tcp \
  -i rtsp://127.0.0.1:18555/youtube-test \
  -frames:v 1 -q:v 2 calibration-cam1.jpg
```

Si la fuente es una camara real, reemplaza la URL por el RTSP de esa camara.

Abre el calibrador en un navegador:

```bash
open tools/calibrate_line.html
```

En Linux puedes usar:

```bash
xdg-open tools/calibrate_line.html
```

Uso:
- Carga `calibration-cam1.jpg`.
- Pega el `CAMERAS_JSON` actual desde Coolify.
- Ajusta `Ancho objetivo` y `Alto objetivo` a los valores de `settings.source_width` y `settings.source_height` del contador. Por defecto son `1280` y `720`.
- Click 1: punto inicial de la linea.
- Click 2: punto final de la linea.
- Click 3: direccion de entrada de la flecha.
- Copia el `CAMERAS_JSON` generado.
- Pega el resultado en la variable `CAMERAS_JSON` de Coolify y haz redeploy del `people-counter`.

Archivo del calibrador:

```text
tools/calibrate_line.html
```

### Calibrar con ventana OpenCV

Este flujo queda como alternativa local. Requiere OpenCV funcionando dentro de la imagen y una terminal con `DISPLAY`.

Obtiene el contenedor, la imagen y el volumen persistente usados por Coolify:

```bash
PC=$(docker ps -q --filter name=people-counter | head -1)
IMAGE=$(docker inspect -f '{{.Config.Image}}' "$PC")
DATA_VOL=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$PC")
```

Detiene temporalmente el contador y permite que el contenedor abra una ventana X11:

```bash
docker stop "$PC"
xhost +si:localuser:root
```

Inicia el modo de calibracion usando la misma imagen y el mismo volumen:

```bash
docker run --rm -it \
  --network host \
  --runtime nvidia \
  --privileged \
  -e DISPLAY="$DISPLAY" \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  -v "$DATA_VOL":/data \
  --entrypoint /usr/bin/python3 \
  "$IMAGE" \
  /app/people_counter_jetson.py \
  --config /data/config.json \
  --calibrate
```

Si OpenCV falla por `libmp3lame.so.0`, usa este comando alternativo. Regenera el linker cache dentro del contenedor temporal antes de iniciar la calibracion:

```bash
docker run --rm -it \
  --network host \
  --runtime nvidia \
  --privileged \
  -e DISPLAY="$DISPLAY" \
  -e LD_LIBRARY_PATH=/usr/lib:/usr/lib/aarch64-linux-gnu:/lib/aarch64-linux-gnu:/usr/lib/aarch64-linux-gnu/nvidia \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  -v "$DATA_VOL":/data \
  --entrypoint bash \
  "$IMAGE" -lc '\
    printf "%s\n" /usr/lib /usr/lib/aarch64-linux-gnu /lib/aarch64-linux-gnu /usr/lib/aarch64-linux-gnu/nvidia > /etc/ld.so.conf.d/ai-people-counting-aarch64.conf; \
    lame_lib=$(find /usr/lib /lib -name "libmp3lame.so.0*" 2>/dev/null | head -n 1 || true); \
    if [ -n "$lame_lib" ] && [ ! -e /usr/lib/libmp3lame.so.0 ]; then ln -s "$lame_lib" /usr/lib/libmp3lame.so.0; fi; \
    ldconfig || true; \
    /usr/bin/python3 /app/people_counter_jetson.py --config /data/config.json --calibrate'
```

Si aparece `Falta dependencia: python3-opencv`, diagnostica el import de OpenCV dentro de la imagen desplegada:

```bash
docker run --rm \
  --entrypoint /usr/bin/python3 \
  "$IMAGE" -c 'import cv2; print(cv2.__version__)'
```

Para listar librerias dinamicas faltantes:

```bash
docker run --rm \
  --entrypoint bash \
  "$IMAGE" -lc 'ldd /usr/lib/python3/dist-packages/cv2*.so | grep "not found" || true'
```

Para diagnosticar `libmp3lame` especificamente:

```bash
docker run --rm \
  --entrypoint bash \
  "$IMAGE" -lc 'find /usr/lib /lib -name "libmp3lame.so.0*" -print; ldconfig -p | grep libmp3lame || true; echo "$LD_LIBRARY_PATH"; /usr/bin/python3 -c "import cv2; print(cv2.__version__)"'
```

Si el error menciona una libreria `.so` faltante, agrega el paquete APT correspondiente al `Dockerfile`, haz push y redeploy.

Controles:
- Click izquierdo: marca los dos extremos de la linea.
- `W/A/S/D`: define la direccion de entrada de la flecha.
- Click derecho: define una direccion de entrada personalizada.
- `R`: reinicia el dibujo.
- `Enter`: guarda la calibracion.
- `Esc`: cancela.

Extrae el `CAMERAS_JSON` actualizado:

```bash
docker run --rm \
  -v "$DATA_VOL":/data \
  --entrypoint /usr/bin/python3 \
  "$IMAGE" -c \
  'import json; print(json.dumps(json.load(open("/data/config.json"))["cameras"], separators=(",",":")))'
```

Copia el resultado en la variable `CAMERAS_JSON` de Coolify y haz redeploy del `people-counter`. Esto evita que un futuro arranque restaure la linea anterior desde las variables de entorno.

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
- Si una cámara RTSP deja de responder, el proceso termina después de 10 segundos y Docker reinicia el contenedor. Esto crea un contexto CUDA nuevo y evita reutilizar NvDCF después de un `EOS`. Los intentos quedan visibles en los logs de Coolify.
- En JetPack 6.2 con DeepStream 7.1, los elementos `nvvideoconvert` usan `copy-hw=2` (VIC) para evitar el fallo conocido `cudaErrorIllegalAddress` durante copias de memoria.
- El `Dockerfile` instala el wheel oficial `pyds 1.2.0` para Jetson, compatible con DeepStream 7.1. Si `import pyds` falla tras un redeploy, confirma que Coolify haya reconstruido la imagen desde el commit más reciente y no esté reutilizando una imagen anterior.


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
