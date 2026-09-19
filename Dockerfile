# SIH26123 — Edge-AI Distributed Fleet Coordination.
# The simulation and the dashboard server are pure standard library, so the image
# needs nothing but a Python base: no pip install, no build step, no node.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    MAX_SESSIONS=32 \
    SESSION_TTL_S=3600

WORKDIR /app

# Source first, app after: deployment config changes do not re-copy the tree.
COPY simulation ./simulation
COPY frontend ./frontend
COPY scripts ./scripts
COPY docs ./docs
COPY results ./results
COPY tests ./tests
COPY README.md LICENSE ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 fleet \
    && chown -R fleet:fleet /app
USER fleet

EXPOSE 8000

# The container port follows $PORT, so the same image fits Docker, Render, Fly.
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=3).read()"

CMD ["python", "scripts/run_web.py", "--host", "0.0.0.0"]
