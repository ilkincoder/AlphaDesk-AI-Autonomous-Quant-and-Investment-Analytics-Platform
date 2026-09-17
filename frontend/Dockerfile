FROM node:24-alpine

WORKDIR /app

# Dependencies are installed in their own layer, before the source is copied in, so
# editing a component does not re-run the install. Same shape as backend/Dockerfile.
#
# npm ci, not npm install: it installs exactly what package-lock.json records and
# fails if the two have drifted, which is the point of committing a lockfile.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .

EXPOSE 5173

# The dev server, not a production build: this container exists to give hot reload.
# --host 0.0.0.0 is set here rather than in vite.config.ts so that running
# `npm run dev` on the host still binds to localhost only.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
