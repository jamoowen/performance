FROM python:3.14.8-slim AS runtime

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
COPY benchmarks/http/ramp/python/pyproject.toml benchmarks/http/ramp/python/uv.lock ./
RUN pip install --no-cache-dir uv==0.12.22 && uv sync --frozen --no-dev
COPY benchmarks/http/ramp/python/app.py benchmarks/http/ramp/python/launcher.py ./
RUN useradd --system --uid 10001 benchmark && mkdir /data && chown benchmark:benchmark /data /app
USER benchmark
EXPOSE 8080
CMD [".venv/bin/python", "launcher.py"]
