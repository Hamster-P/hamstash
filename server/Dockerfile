FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 默认 17420(避开 qBittorrent WebUI 默认的 8080),可用 SERVER_PORT 覆盖
ENV SERVER_PORT=17420
EXPOSE 17420

# 容器里要监听 0.0.0.0,不走 run_service.py(它绑 127.0.0.1);端口取 SERVER_PORT
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${SERVER_PORT:-17420}"]