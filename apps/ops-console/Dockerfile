FROM node:22-slim AS deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS runner

WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3001

COPY --from=deps --chown=node:node /app/node_modules ./node_modules
COPY --chown=node:node . .

RUN chown node:node /app

USER node

RUN npm run build -- --webpack

EXPOSE 3001

CMD ["sh", "-c", "npm run validate:runtime && npm run start -- --hostname 0.0.0.0"]
