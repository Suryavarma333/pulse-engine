FROM node:22-alpine AS dependencies
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

FROM node:22-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_PULSE_API_URL=http://localhost:8100
ARG NEXT_PUBLIC_PULSE_POLL_MS=2000
ENV NEXT_PUBLIC_PULSE_API_URL=${NEXT_PUBLIC_PULSE_API_URL} \
    NEXT_PUBLIC_PULSE_POLL_MS=${NEXT_PUBLIC_PULSE_POLL_MS}
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
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
