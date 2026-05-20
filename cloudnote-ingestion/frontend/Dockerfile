# Stage 1: Compile and Build React SPA
FROM node:20-alpine AS builder

WORKDIR /app

# Copy dependency configs first for caching optimization
COPY frontend/package*.json ./
RUN npm ci

# Copy all source files
COPY frontend/ .

# Compile Vite production bundle (outputs to /app/dist)
RUN npm run build

# Stage 2: Serve using Nginx
FROM nginx:1.25-alpine

# Copy custom Nginx configuration
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

# Copy production assets from builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose port
EXPOSE 80

# Run Nginx in foreground
CMD ["nginx", "-g", "daemon off;"]
