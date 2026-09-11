# One image that serves the whole site: the API, the scheduled jobs and the
# built frontend.
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

# Installed from pyproject.toml rather than a list repeated here, so there is
# one place that says what this depends on. It is four packages, so the layer
# is small enough that rebuilding it on a code change costs little.
COPY backend/ ./backend/
RUN pip install --no-cache-dir ./backend

COPY --from=frontend /build/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1 \
    FPLAI_FRONTEND_DIST=/srv/frontend/dist \
    FPLAI_DB=/data/fplai.sqlite3 \
    FPLAI_CACHE=/data/cache \
    PORT=8000

# The jobs run in this process. There is nowhere else for them to run: the
# database lives on a disk, and a disk attaches to one service only.
ENV FPLAI_SCHEDULER=1

# The season's whole record lives in that one file, so it needs to outlive the
# container. Mount a volume here.
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"

CMD ["python", "-m", "fplai.api.app"]
