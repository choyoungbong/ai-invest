FROM node:20-alpine AS base
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .

# 빌드 시점에 API URL 주입
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

EXPOSE 3000
CMD ["npm", "start"]
