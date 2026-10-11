# Build context: repo root (npm workspaces).
FROM node:20-alpine AS build
WORKDIR /repo
COPY package.json package-lock.json tsconfig.base.json ./
COPY packages/ui packages/ui
COPY apps/web apps/web
# Measured numbers shown by the UI (written by `make evaluation`), baked in at build time.
COPY docs/eval-report/metrics.json docs/eval-report/wells.json docs/eval-report/
COPY docs/eval-report/timelines docs/eval-report/timelines
COPY tests/e2e/package.json tests/e2e/package.json
RUN npm ci --no-audit --no-fund
ARG VITE_API_URL=https://localhost:5173/api
ARG VITE_OIDC_ISSUER=https://localhost:5173/auth/realms/wells
ENV VITE_API_URL=$VITE_API_URL VITE_OIDC_ISSUER=$VITE_OIDC_ISSUER
RUN npm run build -w apps/web

FROM nginx:1.29-alpine
RUN rm /etc/nginx/conf.d/default.conf
COPY apps/web/nginx.conf /etc/nginx/conf.d/web.conf
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
EXPOSE 8443
