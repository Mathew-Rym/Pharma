FROM node:20-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js ./
# Baileys auth state MUST live on a mounted volume or you re-scan the QR every deploy
ENV AUTH_DIR=/data/auth
VOLUME ["/data"]
EXPOSE 3000
CMD ["node", "index.js"]
