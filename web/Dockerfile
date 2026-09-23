# ---- 构建阶段：只装依赖并产出静态文件 ----
# 固定构建环境，并要求锁文件与 package.json 一致。
FROM node:22.23.2-alpine AS build

WORKDIR /app

# 先只拷依赖清单，利用镜像层缓存：源码变更不会导致重复安装依赖。
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# 再拷构建配置与源码。
COPY tsconfig.json vite.config.ts index.html ./
COPY types ./types
COPY src ./src
COPY public ./public

# 高德「网页端 JS Key」是构建期注入的公开变量（Vite 只把 VITE_* 编进产物）。
# 不传该参数时构建仍然成功，地图区域会显示「未配置地图 Key」，其余功能正常。
# docker-compose.yml 需要在 build.args 里传入同一个值（部署侧文件不在本次改动范围内）。
ARG VITE_AMAP_JS_KEY=
ENV VITE_AMAP_JS_KEY=$VITE_AMAP_JS_KEY

# npm run build = vue-tsc --noEmit && vite build；类型错误会让镜像构建失败。
RUN npm run build

# ---- 运行阶段：Caddy 提供静态文件并反向代理 /api ----
FROM caddy:2.11.4-alpine

COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile

EXPOSE 80
