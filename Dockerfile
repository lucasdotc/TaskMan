# Slim, not Alpine -- all of requirements.txt ships prebuilt wheels for
# Debian-based images (including psycopg[binary], which bundles libpq
# itself), so nothing here needs a C compiler. Alpine's musl libc doesn't
# match those wheels and would fall back to a much slower source build.
FROM python:3.12-slim

WORKDIR /app

# Copied and installed before the rest of the source so Docker's layer
# cache can skip the (slow) pip install on every rebuild -- it only reruns
# when requirements.txt itself changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# -u disables Python's stdout buffering. Without it, log lines can sit in
# a buffer and not show up in `docker compose logs` until it fills up or
# the process exits -- exactly the buffering gotcha we hit testing this
# locally, where a background process looked silent for several seconds
# before its logs actually appeared.
CMD ["python", "-u", "bot.py"]
