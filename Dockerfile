# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo el código
COPY . .

# Puerto gRPC + Flask (opcional)
EXPOSE 50051 5000

# Comando de inicio (ejecuta solo el signaling server)
CMD ["python", "signaling_server.py"]