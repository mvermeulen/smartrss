# Multi-stage build for SmartRSS connected LAN fallback SPA (1c §3.2, T5)
FROM node:22-alpine AS build
WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
RUN mkdir -p /etc/nginx/snippets
COPY deploy/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY deploy/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY deploy/nginx/client-config.json /usr/share/nginx/html/client-config.json
