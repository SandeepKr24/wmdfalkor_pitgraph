# PitGraph web app: FastAPI + static UI. The graph lives on FalkorDB Cloud,
# so this container only needs the app code and credentials (passed via --env-file).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pitgraph/ pitgraph/

# Run as a non-root user.
RUN useradd --create-home pitgraph
USER pitgraph

# Hosts like Render set $PORT; default to 8000 elsewhere.
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn pitgraph.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
