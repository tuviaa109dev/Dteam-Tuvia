# One Dockerfile, several targets (selected per service in docker-compose.yml):
#   backend  - API, worker and migrate services (Python)
#   test     - backend + test dependencies + Tests/
#   frontend - React dashboard built with Vite, served by nginx

# ---- frontend -------------------------------------------------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY App/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY App/frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine AS frontend
COPY App/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /build/dist /usr/share/nginx/html

# ---- backend --------------------------------------------------------------------------------
FROM python:3.12-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY App/backend/requirements.txt .
RUN pip install -r requirements.txt
COPY App/backend/app ./app
RUN useradd --system --uid 1001 app
USER app
EXPOSE 8000
# Overridden per service in docker-compose.yml (api / worker / migrate).
CMD ["python", "-m", "app.api"]

# ---- tests ----------------------------------------------------------------------------------
FROM backend AS test
USER root
COPY App/backend/requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY Tests ./Tests
USER app
CMD ["python", "-m", "pytest", "Tests", "-v", "-p", "no:cacheprovider"]
