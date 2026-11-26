FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    ZIMAGE_PORT=8096 \
    ZIMAGE_OUTPUT_DIR=/var/lib/vast-coldstart/output \
    ZIMAGE_ENGINE=auto \
    ZIMAGE_PRECISION=bf16 \
    ZIMAGE_STEPS=9

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/zimage-coldstart
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY server.py bootstrap.sh ./
RUN chmod +x bootstrap.sh && mkdir -p /var/lib/vast-coldstart/output

EXPOSE 8096
CMD ["bash", "bootstrap.sh"]
