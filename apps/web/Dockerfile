FROM node:22-alpine AS deps
RUN corepack enable && corepack prepare pnpm@9.15.4 --activate
WORKDIR /repo
COPY pnpm-lock.yaml pnpm-workspace.yaml package.json ./
COPY apps/web/package.json apps/web/
COPY packages/contracts/package.json packages/contracts/
RUN pnpm install --frozen-lockfile --filter @warilink/web...

FROM deps AS build
# rewrites はビルド時に確定するため、API の接続先をビルド引数で渡す
ARG API_ORIGIN=http://api:8000
ENV API_ORIGIN=$API_ORIGIN
COPY packages/contracts packages/contracts
COPY apps/web apps/web
RUN pnpm --filter @warilink/web build

FROM node:22-alpine
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build /repo/apps/web/.next/standalone ./
COPY --from=build /repo/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
