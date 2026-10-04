FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABRIDGE_DATA_DIR=/data

# Microsoft ODBC driver for SQL Server is optional; uncomment if you need SQL Server via pyodbc.
# RUN apt-get update && apt-get install -y curl gnupg unixodbc && \
#     curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/ms.gpg && \
#     echo "deb [signed-by=/usr/share/keyrings/ms.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql.list && \
#     apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-drivers.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-drivers.txt

COPY databridge ./databridge
COPY samples ./samples

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

# Traefik reaches the app over a Docker network (private address ranges); only those may set X-Forwarded-*.
# The client address is then taken from the right-most untrusted X-Forwarded-For entry, so clients can't fake it.
# If you publish port 8000 or use another proxy, set DATABRIDGE_TRUSTED_PROXIES to exactly your proxy's address.
ENV DATABRIDGE_TRUSTED_PROXIES="127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-m", "databridge.healthcheck"]
# databridge.serve = uvicorn plus TLS (if configured), proxy trust and websocket limits from DATABRIDGE_* settings
CMD ["python", "-m", "databridge.serve", "--host", "0.0.0.0", "--port", "8000"]
