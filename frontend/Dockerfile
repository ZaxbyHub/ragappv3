# Build stage
FROM node:20-alpine AS build

WORKDIR /app

ENV NODE_ENV=production

COPY package*.json ./

RUN npm ci

COPY . .
COPY scripts/validate_vite_env.mjs /tmp/validate_vite_env.mjs

ARG VITE_APP_BASENAME=/
ARG VITE_API_URL=
ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}
ENV VITE_API_URL=${VITE_API_URL}

RUN node /tmp/validate_vite_env.mjs
RUN npm run build

# Final stage
FROM nginx:alpine

LABEL maintainer="RAGAPP Team"
LABEL description="RAGAPP Frontend - Multi-stage build"

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
