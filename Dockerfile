FROM python:3.10-slim
 
WORKDIR /opt/app
 
# Install Python deps first so this layer is cached when only app code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r /opt/app/requirements.txt
 
# Install packages that we need (node exporter for container metrics on :9100)
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get upgrade -yq ca-certificates && \
    apt-get install -yq --no-install-recommends \
    prometheus-node-exporter && \
    rm -rf /var/lib/apt/lists/*
 
# App code + data/ ticker CSVs
COPY . .
 
# Product identity and ports (my claimed range is 1700-1779).
# Container 1 (API product):   defaults below.
# Container 2 (local product): run with
#   -e PRODUCT_NAME=G17_StockSuggestor2 -e GRADIO_SERVER_PORT=1710 -e METRICS_PORT=1711
ENV PRODUCT_NAME="G17_StockSuggestor"
ENV GRADIO_SERVER_NAME="0.0.0.0"
ENV GRADIO_SERVER_PORT=1700
ENV METRICS_PORT=1701
 
EXPOSE 1700
EXPOSE 1701
EXPOSE 9100
 
CMD bash -c "prometheus-node-exporter --web.listen-address=':9100' & exec python /opt/app/app.py"
