# 阵图 ZhenTu 镜像
#
# 仅复制 zhentu/ 包与依赖，构建上下文为仓库根目录：
#   docker build -f deploy/zhentu/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY zhentu/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY zhentu /app/zhentu

RUN useradd --create-home --uid 10001 biyou \
    && mkdir -p /data \
    && chown -R biyou:biyou /app /data

USER biyou

EXPOSE 4020

CMD ["python", "-m", "uvicorn", "zhentu.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "4020"]
