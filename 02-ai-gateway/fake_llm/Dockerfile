FROM python:3.13-slim
RUN pip install --no-cache-dir "fastapi==0.142.*" "uvicorn==0.54.*" && useradd --uid 10001 app
COPY app.py /app/app.py
WORKDIR /app
USER app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
