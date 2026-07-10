FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Konteynerde dışarıya açılması gerekir; veri kalıcılığı için DATA_DIR volume'u
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    DATA_DIR=/app/data

RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "app.py"]
