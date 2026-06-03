# Image officielle Playwright : Chromium + dépendances système + sandbox inclus.
# Version alignée sur le playwright pip (1.60.0) pour éviter tout mismatch.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7332 \
    HOST=0.0.0.0 \
    HEADLESS=1

# Node.js : requis par core/crypto/encrypt.js (chiffrement cryptico de la charge,
# appelé en sous-processus `node` par le replay). L'image Playwright ne le fournit
# PAS dans le PATH système -> on l'installe explicitement.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Chromium est déjà dans l'image ; on garantit le lien avec la version pip.
RUN playwright install chromium

COPY . .

# L'app démarre via son launcher custom (pool navigateur + lifespan + PORT/HOST),
# PAS via `fastapi run`.
CMD ["python", "main.py"]
