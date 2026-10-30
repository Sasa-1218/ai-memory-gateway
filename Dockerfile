# 用 Python 精简镜像（体积小，部署快）
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存加速
COPY requirements.txt .
COPY wheels/ /wheels/
RUN --mount=type=cache,target=/root/.cache/pip set -eux; \
    mkdir -p /tmp/jieba-src; \
    tar -xzf /wheels/jieba-0.42.1.tar.gz -C /tmp/jieba-src --strip-components=1; \
    SITE_PACKAGES="$(python -c 'import site; print(site.getsitepackages()[0])')"; \
    cp -a /tmp/jieba-src/jieba "$SITE_PACKAGES/jieba"; \
    grep -v '^jieba==' requirements.txt > /tmp/requirements-no-jieba.txt; \
    (pip install \
        --timeout 120 \
        --retries 3 \
        -i "https://pypi.org/simple" \
        -r /tmp/requirements-no-jieba.txt \
     || pip install \
        --timeout 120 \
        --retries 5 \
        -i "${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}" \
        -r /tmp/requirements-no-jieba.txt); \
    python -c "import jieba; print('jieba import ok')"

# 复制项目文件
COPY . .

# 端口（Render 等平台会通过 PORT 环境变量分配）
# 默认 8000
ENV PORT=8000

# 启动网关
CMD ["python", "main.py"]
