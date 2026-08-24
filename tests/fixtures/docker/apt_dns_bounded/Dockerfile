FROM python:3.11-slim
RUN RES_OPTIONS="attempts:1 timeout:2" apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 update && \
    RES_OPTIONS="attempts:1 timeout:2" apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 install -y git
