FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY embyserver ./embyserver
EXPOSE 8096
VOLUME ["/config", "/media"]
CMD ["python", "-m", "embyserver", "-c", "/config/config.yaml"]
