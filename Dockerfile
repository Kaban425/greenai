# Целевая среда заказчика — МосТех.ОС (Linux на базе Ubuntu).
# Образ собирается на slim-базе Debian, совместимой по окружению.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="GreenAI" \
      org.opencontainers.image.description="Генеративный дизайн городского озеленения" \
      org.opencontainers.image.licenses="MIT"

# ODA File Converter читает DWG: весь набор «Пилотный проект 20 улиц» —
# DWG. Бесплатная версия с сайта Open Design Alliance; без неё образ
# принимает только DXF (сборка с --build-arg ODA_URL= пропускает шаг).
# Конвертер — приложение Qt, в контейнере ему нужен виртуальный дисплей.
ARG ODA_URL=https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_27.1.deb

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-c1v5 libgomp1 ca-certificates curl xvfb xauth \
        libxcb-util1 libgl1 libglib2.0-0 libfontconfig1 libdbus-1-3 \
        libxkbcommon0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxcb-xinerama0 libxcb-xinput0 libxcb-cursor0 libegl1 \
    && if [ -n "$ODA_URL" ]; then \
         curl -fsSL -o /tmp/oda.deb "$ODA_URL" \
         && apt-get install -y --no-install-recommends /tmp/oda.deb \
         && rm /tmp/oda.deb \
         && ln -sf /usr/lib/x86_64-linux-gnu/libxcb-util.so.1 \
                   /usr/lib/x86_64-linux-gnu/libxcb-util.so.0 ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Запуск конвертера без экрана: через виртуальный X-сервер. Предупреждения
# Qt («QStandardPaths: XDG_RUNTIME_DIR…») уходят в журнал: ezdxf считает
# любой вывод в stderr ошибкой, хотя файл сконвертирован. Успех расчёт
# проверяет по появлению DXF.
RUN printf '#!/bin/sh\nexe=$(command -v ODAFileConverter || ls /usr/bin/ODAFileConverter_* /opt/ODAFileConverter*/ODAFileConverter 2>/dev/null | head -1)\nmkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"\nxvfb-run -a "$exe" "$@" >>/tmp/odafc.log 2>&1\nexit 0\n' \
        > /usr/local/bin/odafc-headless \
    && chmod +x /usr/local/bin/odafc-headless
ENV ODAFC_PATH=/usr/local/bin/odafc-headless XDG_RUNTIME_DIR=/tmp/runtime-root

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config ./config
COPY src ./src
COPY data/sample.dxf ./data/sample.dxf

# Данные и результаты монтируются снаружи
RUN mkdir -p /app/input /app/output /app/cache
VOLUME ["/app/input", "/app/output"]

# По умолчанию поднимается веб-интерфейс; API и Swagger на /docs.
ENV GREENAI_HOST=0.0.0.0 GREENAI_PORT=8000 GREENAI_OPEN=0 PYTHONIOENCODING=utf-8
EXPOSE 8000
CMD ["python", "src/webapp.py"]
