FROM python:3.12-alpine
COPY exporter.py /app/exporter.py
EXPOSE 9719
ENTRYPOINT ["python3", "/app/exporter.py"]
