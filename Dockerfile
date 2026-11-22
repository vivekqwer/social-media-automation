FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig ca-certificates curl unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# free person-cutout model for face thumbnails (no AI service)
RUN mkdir -p models && curl -fsSL -o models/u2net_human_seg.onnx https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx \
    && curl -fsSL -o models/face_detection_yunet.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
COPY app app
COPY static static
COPY fonts fonts
# the helper program customers download from their dashboard is built from these files
COPY worker worker
# make the bundled fonts (Noto Devanagari, Anton) available to libass font fallback too
RUN mkdir -p /usr/share/fonts/truetype/clipstudio && cp fonts/*.ttf /usr/share/fonts/truetype/clipstudio/ && fc-cache -f
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
