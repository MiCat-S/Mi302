FROM python:3.12-slim
# ffprobe：探測 strm 指向的影片，產生媒體資訊（解析度、HDR、音軌、字幕軌）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# 國內網路可以用 pip 鏡像：docker compose build --build-arg PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_MIRROR=
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_MIRROR:+-i "$PIP_MIRROR"} -r requirements.txt
COPY embyserver ./embyserver
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
# 以 /config 為工作目錄，設定檔裡的相對路徑（例如 data_dir: ./data）會落在掛載的資料夾
WORKDIR /config
EXPOSE 8096
VOLUME ["/config", "/media"]
CMD ["python", "-m", "embyserver", "-c", "/config/config.yaml"]
