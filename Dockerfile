# PackSeed —— 纯标准库，无需 pip 安装任何依赖
FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY packseed.py /app/packseed.py

EXPOSE 2470

CMD ["python", "/app/packseed.py"]
