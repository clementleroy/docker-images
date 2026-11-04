FROM python:3.13.9-alpine3.22
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py air_engine.py configuration.py ./
CMD ["python", "app.py"]
