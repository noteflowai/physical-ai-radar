# Trusted tools only. Product dependencies are installed from the approved base commit.
FROM node@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS node
FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update && apt-get install -y --no-install-recommends \
 git curl unzip php-cli php-xml php-mbstring shellcheck \
 libgl1 libegl1 libosmesa6 libglib2.0-0 libnss3 libatk-bridge2.0-0 \
 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
 libgbm1 libasound2t64 libcups2 fonts-liberation \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g pnpm@12.3.4 \
 && pip install --no-cache-dir pytest==8.4.2 ruff==0.15.6 build==1.4.0 \
 && useradd --uid 1000 --create-home worker
ENV CI=1 PIP_NO_INPUT=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
 PYTHONDONTWRITEBYTECODE=1 MUJOCO_GL=egl
WORKDIR /workspace
