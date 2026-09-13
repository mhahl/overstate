FROM node:24-slim AS css
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY assets ./assets
COPY overstate_ui ./overstate_ui
RUN npm run build:css -- --minify

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY overstate_ui ./overstate_ui
COPY --from=css /build/overstate_ui/static/app.css ./overstate_ui/static/app.css
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
RUN pip install --no-cache-dir -r requirements.lock .

ENV PYTHONUNBUFFERED=1
ENV TLS_CERT=/srv/tls/app.crt
ENV TLS_KEY=/srv/tls/app.key
EXPOSE 8000
CMD ["sh", "scripts/docker-entrypoint.sh"]
