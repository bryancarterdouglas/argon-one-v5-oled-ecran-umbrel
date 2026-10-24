# Petit programme pour l'ecran OLED du boitier Argon ONE V5 (voir README.md)
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-libgpiod python3-pil fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY ecran.py test_ecran.py /app/
CMD ["python3", "/app/ecran.py", "run"]
