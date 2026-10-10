# syntax=docker/dockerfile:1.7
#
# SMM Control Center web image: the React production build behind nginx,
# which also reverse-proxies /api and /health to the FastAPI container so
# the browser sees one origin (no CORS, first-party session cookie).
#
#   docker build -t ai-smm-web:<tag> web/
#
# Base images are pinned by digest, as for the Python image. Node exists
# only in the builder: the final image is nginx and the static files.

# node:24-alpine == Node 24.21.0 (web/.nvmrc: 24), pinned by digest
FROM node@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS builder

WORKDIR /web

# Dependencies first: this layer is reused whenever only source changes.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

COPY . .

# Same-origin build: the bundle calls /api relative to the page. Set here,
# not left to the context, so no API address can be baked into the
# assets. A variable already in the environment takes precedence over any
# .env file Vite would otherwise read.
ENV VITE_API_BASE_URL=

RUN npm run build


# nginxinc/nginx-unprivileged:stable-alpine == nginx 1.30.5, pinned by
# digest. Runs as uid 101 and listens on 8080, so the container needs no
# capabilities and works with a read-only root filesystem.
FROM nginxinc/nginx-unprivileged@sha256:15c994d10d6d78658721c3bcafff14cb281fba2a4bdf9d5ba92c416a472516e3 AS runtime

# Replaces the stock server block (and its directory of sample pages).
COPY --chown=root:root nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --chown=root:root nginx/smm-proxy.inc /etc/nginx/snippets/smm-proxy.inc
COPY --from=builder --chown=root:root /web/dist /srv/smm-web

# Fails the build, not the deployment, on a config error. The API host is
# resolved per request, so this needs no running API.
RUN nginx -t

EXPOSE 8080

# The static site only: whether the API behind it is up is a separate
# question, which the compose healthcheck asks through the proxy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/"]
