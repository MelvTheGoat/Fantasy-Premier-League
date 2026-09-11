# One image that serves the whole site: the API and the built frontend.
#
# The frontend is compiled in a throwaway stage so Node is not carried into the
# runtime image, and the result is copied next to the backend, which serves it.

FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim
WORKDIR /srv

# Installed before the source so a code change does not reinstall the world.
COPY backend/pyproject.toml ./backend/
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" "pulp>=2.9"

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

ENV PYTHONPATH=/srv/backend \
    PYTHONUNBUFFERED=1 \
    FPLAI_FRONTEND_DIST=/srv/frontend/dist \
    FPLAI_DB=/data/fplai.sqlite3 \
    FPLAI_CACHE=/data/cache \
    PORT=8000

# The season's whole record lives in this one file, so it needs to outlive the
# container. Mount a volume here.
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"

CMD ["python", "-m", "fplai.api.app"]
