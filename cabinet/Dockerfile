# Сборка в два этапа: в рантайм уходит только standalone-сборка Next,
# без исходников и без полного node_modules.
FROM node:22-alpine AS build

WORKDIR /srv

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# Правила проксирования Next запекает при сборке. На проде /api до кабинета не
# доходит, его забирает Caddy, но адрес нужен, чтобы сборка не падала.
ENV API_ORIGIN=http://bot:8000
RUN npm run build

FROM node:22-alpine

WORKDIR /srv
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Контейнер не под root, как и бот.
RUN addgroup -g 10002 cabinet && adduser -u 10002 -G cabinet -D cabinet

COPY --from=build --chown=cabinet:cabinet /srv/.next/standalone ./
COPY --from=build --chown=cabinet:cabinet /srv/.next/static ./.next/static

USER cabinet
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"

CMD ["node", "server.js"]
