FROM nvcr.io/nvidia/deepstream:7.1-samples-multiarch

ENV PYTHONUNBUFFERED=1 \
    APP_DIR=/app \
    CACHE_ROOT=/root/.cache/ai-people-counting \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    LD_LIBRARY_PATH=/usr/lib/aarch64-linux-gnu:/lib/aarch64-linux-gnu:/usr/lib/aarch64-linux-gnu/nvidia

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-gi \
        python3-opencv \
        libpython3.10 \
        ffmpeg \
        libx264-163 \
        libmp3lame0 \
        gir1.2-gst-rtsp-server-1.0 \
        gstreamer1.0-tools \
        gstreamer1.0-rtsp \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        curl \
        ca-certificates \
    && lame_lib="$(find /usr/lib /lib -name 'libmp3lame.so.0*' | head -n 1)" \
    && test -n "${lame_lib}" \
    && lame_dir="$(dirname "${lame_lib}")" \
    && printf '%s\n' "${lame_dir}" /usr/lib/aarch64-linux-gnu /lib/aarch64-linux-gnu /usr/lib/aarch64-linux-gnu/nvidia > /etc/ld.so.conf.d/ai-people-counting-aarch64.conf \
    && if [ ! -e "${lame_dir}/libmp3lame.so.0" ]; then ln -s "$(basename "${lame_lib}")" "${lame_dir}/libmp3lame.so.0"; fi \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

RUN if [ -x /opt/nvidia/deepstream/deepstream/user_additional_install.sh ]; then \
        /opt/nvidia/deepstream/deepstream/user_additional_install.sh; \
    fi \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

RUN /usr/bin/python3 -c "import cv2; print('OpenCV OK:', cv2.__version__)"

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt \
    || python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt

ARG PYDS_VERSION=1.2.0
ARG PYDS_WHEEL=pyds-1.2.0-cp310-cp310-linux_aarch64.whl
RUN curl -fL \
        "https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v${PYDS_VERSION}/${PYDS_WHEEL}" \
        -o "/tmp/${PYDS_WHEEL}" \
    && (python3 -m pip install --no-cache-dir "/tmp/${PYDS_WHEEL}" \
        || python3 -m pip install --break-system-packages --no-cache-dir "/tmp/${PYDS_WHEEL}") \
    && rm -f "/tmp/${PYDS_WHEEL}" \
    && ldconfig \
    && ldconfig -p | grep -q 'libpython3.10.so.1.0'

COPY . .

RUN chmod +x scripts/setup_peoplenet.sh scripts/docker-entrypoint.sh \
    && mkdir -p /data "${CACHE_ROOT}/models/peoplenet" "${CACHE_ROOT}/engines"

VOLUME ["/data", "/root/.cache/ai-people-counting"]

ENTRYPOINT ["scripts/docker-entrypoint.sh"]
CMD []
