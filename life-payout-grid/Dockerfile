FROM node:20-slim

# The admin upload route shells out to a Python parser for the monthly
# Excel workbook (pandas/openpyxl) -- Node alone can't read .xlsx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/requirements.txt scripts/requirements.txt
RUN python3 -m venv scripts/venv \
    && scripts/venv/bin/pip install --no-cache-dir -r scripts/requirements.txt

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

ENV NODE_ENV=production
EXPOSE 3000
CMD ["npm", "run", "start"]
