FROM python:3.12.10-slim-bookworm

# 使用国内 pip 源,可通过 --build-arg 覆盖
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.cloud.tencent.com

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy requirements files
COPY requirements.txt requirements-dev.txt ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-dev.txt

# Copy the application
COPY . .

# Normalize CRLF (Windows checkout) so shebangs survive; runner re-execs main.py directly
RUN find /app -type f \( -name '*.py' -o -name '*.sh' \) -print0 | xargs -0 sed -i 's/\r$//' \
    && chmod +x /app/main.py

# Create directories for persistent data
RUN mkdir -p /app/data /app/binlog

ENTRYPOINT ["python3", "/app/main.py"]

# Default command (can be overridden in docker-compose)
CMD ["--help"]
