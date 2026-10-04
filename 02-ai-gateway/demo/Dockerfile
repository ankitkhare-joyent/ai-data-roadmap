FROM python:3.13-slim
RUN pip install --no-cache-dir "anthropic==1.11.*" "httpx==0.28.*" && useradd --uid 10001 app
COPY gateway_demo.py /app/gateway_demo.py
WORKDIR /app
USER app
ENTRYPOINT ["python", "gateway_demo.py"]
