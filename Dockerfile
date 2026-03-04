FROM python:3.11-slim

WORKDIR /app

# System deps for sounddevice (unused in Docker, but required by pip install)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libportaudio2 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Railway injects PORT at runtime (default 5000)
ENV PORT=5000
EXPOSE ${PORT}

# Run complex_agent (full-featured server with dashboard, tools, DB).
# Override with ENTRY_SCRIPT env var to use a different script.
CMD ["sh", "-c", "python ${ENTRY_SCRIPT:-complex_agent.py}"]
