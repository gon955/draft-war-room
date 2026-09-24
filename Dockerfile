# syntax=docker/dockerfile:1

# 3.12-slim matches CI's python-version and pyproject's target-version = "py312",
# so the image runs the interpreter the 565 tests were green against. `slim`
# rather than `alpine`: Alpine is musl, and every wheel this project needs is
# built for manylinux/glibc — on Alpine pip falls back to compiling from source
# and the build needs a toolchain it should not have to carry.
FROM python:3.12-slim

# MALLOC_ARENA_MAX: the biggest memory lever in this image, and not a Python
#   setting at all. glibc gives each thread its own malloc arena and does not
#   return memory freed in one of them to the OS. Every request here runs on an
#   AnyIO worker thread (the routes are sync `def`s) and a login allocates a
#   ~19 MiB argon2 arena on whichever thread serves it — so after a burst each
#   of the 32 worker threads sits on a 19 MiB hole it will never give back and
#   rarely reuse. Measured, under the same 120 logins:
#
#       unset                455 MiB RSS, and it never came back down
#       MALLOC_ARENA_MAX=2    94 MiB RSS
#
#   Both runs capped concurrent hashes at 4, so the whole difference is
#   retention rather than concurrency. On the 512 MB machine fly.toml asks for,
#   the first number is most of the budget held by memory nobody is using.
#
# PYTHONDONTWRITEBYTECODE: no .pyc litter in a layer that is read-only anyway.
# PYTHONUNBUFFERED: without it Python block-buffers stdout when it is a pipe,
#   which is exactly what a platform's log collector is — so logs arrive in
#   4 KB bursts, or not at all when a crash loses the buffer.
ENV MALLOC_ARENA_MAX=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source, so editing a route does not re-resolve and
# re-download 34 packages. This layer only rebuilds when the lock file changes.
#
# No build-essential and no libpq-dev: psycopg[binary] bundles its own libpq,
# and cryptography, pydantic-core and argon2-cffi-bindings all ship manylinux
# wheels. Nothing here compiles, which is why there is no separate build stage.
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# Application code and the migration tooling. alembic/env.py imports warroom.db
# for Settings and Base.metadata, so the package has to be present for a
# migration to run — which is what lets the release step below use this image.
COPY alembic.ini .
COPY alembic/ alembic/
COPY warroom/ warroom/

# Run unprivileged. If the process is ever compromised it should not own the
# filesystem it is standing on.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

# sh -c so ${PORT} expands — Railway and Render inject it, Fly does not.
# `exec` is the load-bearing word: without it PID 1 is the shell, SIGTERM stops
# at the shell, uvicorn never gets it, and the platform kills the container
# after its grace period instead of letting in-flight requests finish.
#
# --proxy-headers so uvicorn reads X-Forwarded-Proto/For from the platform's
# edge instead of believing every request arrived over plain HTTP from the
# load balancer's own IP.
CMD ["sh", "-c", "exec uvicorn warroom.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
