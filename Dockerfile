# Playwright's official image ships Chromium + all system deps preinstalled,
# which is exactly what the posting engines need. Ideal for Railway.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway provides $PORT at runtime; app/main.py reads it from the environment
# directly, so this works whether or not a shell expands variables.
ENV PORT=8000
CMD ["python", "-m", "app.main"]
