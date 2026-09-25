FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY embyserver ./embyserver
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
# 以 /config 為工作目錄，設定檔裡的相對路徑（例如 data_dir: ./data）會落在掛載的資料夾
WORKDIR /config
EXPOSE 8096
VOLUME ["/config", "/media"]
CMD ["python", "-m", "embyserver", "-c", "/config/config.yaml"]
