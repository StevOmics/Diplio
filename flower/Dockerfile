FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 5555

CMD ["celery", "-A", "app.celery_app", "flower", "--port=5555"]
