# syntax=docker/dockerfile:1
# ── Build stage ──────────────────────────────────────────────────────────────
FROM node:22-slim AS builder
WORKDIR /workspace

# Install pnpm
RUN npm install -g pnpm@9

# Copy workspace manifests first (layer caching)
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY tsconfig.base.json ./

# Copy only the packages the api-server depends on
COPY lib/db/           lib/db/
COPY lib/api-zod/      lib/api-zod/
COPY artifacts/api-server/ artifacts/api-server/

# Install all dependencies
RUN pnpm install --no-frozen-lockfile --ignore-scripts=false

# Build
RUN pnpm --filter @workspace/api-server run build

# `pnpm deploy` creates a self-contained directory with all symlinks resolved —
# this is the only reliable way to get a flat node_modules across Docker stages.
RUN pnpm --filter @workspace/api-server --prod deploy /deploy

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM node:22-slim AS runner
WORKDIR /app

ENV NODE_ENV=production

# Copy the flat, self-contained node_modules from pnpm deploy
COPY --from=builder /deploy/node_modules   ./node_modules

# Copy built output and static assets
COPY --from=builder /workspace/artifacts/api-server/dist           ./dist
COPY --from=builder /workspace/artifacts/api-server/public         ./public
COPY --from=builder /workspace/artifacts/api-server/downloads      ./downloads

EXPOSE 3000
CMD ["node", "--enable-source-maps", "./dist/index.mjs"]
