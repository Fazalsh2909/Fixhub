# Build the React IDE, then serve the static dist via nginx.
FROM node:20-alpine AS build
WORKDIR /ui
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . ./
RUN npm run build

FROM nginx:alpine
COPY --from=build /ui/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
