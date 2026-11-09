# syntax=docker/dockerfile:1
# The web app's production image: Next.js's standalone server on Node.js 26. Build
# from the repository root:
#   docker build --file apps/web/Dockerfile .

FROM node:26.13.0-trixie-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS build
ENV NEXT_TELEMETRY_DISABLED=1
# Node.js 25 and later no longer ship corepack, so install the pnpm that package.json pins.
RUN npm install --global pnpm@12.18.0
WORKDIR /repo

# Dependencies first. This layer is reused until the lockfile or a manifest changes.
# --no-runtime: this image already has the Node.js version that package.json pins.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile --no-runtime --filter @actio/web --store-dir /pnpm/store

COPY apps/web/ apps/web/
WORKDIR /repo/apps/web
RUN node_modules/.bin/next build

FROM node:26.13.0-trixie-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
# The standalone server and the static assets it serves. The files stay owned by root,
# so the app can't modify its own code.
COPY --from=build /repo/apps/web/.next/standalone ./
COPY --from=build /repo/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
