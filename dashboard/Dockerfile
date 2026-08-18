FROM node:22-alpine AS dependencies
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN corepack enable && pnpm build

FROM node:22-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app
RUN addgroup --system pulse && adduser --system --ingroup pulse pulse
COPY --from=build --chown=pulse:pulse /app/.next/standalone ./
COPY --from=build --chown=pulse:pulse /app/.next/static ./.next/static
USER pulse
EXPOSE 3000
CMD ["node", "server.js"]
