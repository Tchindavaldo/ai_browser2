# Image officielle Playwright : Chromium + dépendances système + sandbox + Node
# (requis par core/crypto/encrypt.js) déjà inclus. Version alignée sur le
# playwright pip (1.60.0) pour éviter tout mismatch binaire/lib.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7332 \
    HOST=0.0.0.0 \
    HEADLESS=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Chromium est déjà dans l'image ; on garantit le lien avec la version pip.
RUN playwright install chromium

COPY . .

# L'app démarre via son launcher custom (pool navigateur + lifespan + PORT/HOST),
# PAS via `fastapi run`.
CMD ["python", "main.py"]
