FROM python:3.12-alpine

RUN pip install --no-cache-dir aiohttp==3.10.5

COPY rootfs/app /app
WORKDIR /app

CMD ["python", "pairing.py"]
