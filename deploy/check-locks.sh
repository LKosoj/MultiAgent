#!/bin/sh
set -eu

if [ "$(uv --version)" != "uv 0.10.10" ]; then
    echo "uv 0.10.10 is required" >&2
    exit 2
fi
if [ "$(node --version)" != "v24.13.0" ]; then
    echo "Node v24.13.0 is required" >&2
    exit 2
fi
if [ "$(npm --version)" != "11.6.2" ]; then
    echo "npm 11.6.2 is required" >&2
    exit 2
fi

temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT INT TERM

uv pip compile requirements.txt \
    --output-file "$temporary_directory/requirements.lock" \
    --generate-hashes \
    --python-version 3.12 \
    --python-platform x86_64-manylinux_2_28 \
    --exclude-newer 2026-07-12T00:00:00Z \
    --torch-backend cpu \
    --emit-index-url \
    --custom-compile-command \
    'uv==0.10.10 pip compile requirements.txt --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28 --exclude-newer 2026-07-12T00:00:00Z --torch-backend cpu --emit-index-url --output-file requirements.lock; add PyTorch CPU extra-index' \
    --no-progress
sed -i \
    '/^--index-url https:\/\/pypi.org\/simple$/a --extra-index-url https://download.pytorch.org/whl/cpu' \
    "$temporary_directory/requirements.lock"

uv pip compile requirements-db-optional.txt \
    --output-file "$temporary_directory/requirements-db-optional.lock" \
    --generate-hashes \
    --python-version 3.12 \
    --python-platform x86_64-manylinux_2_28 \
    --exclude-newer 2026-07-12T00:00:00Z \
    --custom-compile-command \
    'uv==0.10.10 pip compile requirements-db-optional.txt --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28 --exclude-newer 2026-07-12T00:00:00Z --output-file requirements-db-optional.lock' \
    --no-progress

cmp requirements.lock "$temporary_directory/requirements.lock"
cmp requirements-db-optional.lock \
    "$temporary_directory/requirements-db-optional.lock"
cp frontend/client/package.json "$temporary_directory/package.json"
cp frontend/client/package-lock.json "$temporary_directory/package-lock.json"
npm ci --ignore-scripts --package-lock-only --prefix frontend/client
cmp frontend/client/package.json "$temporary_directory/package.json"
cmp frontend/client/package-lock.json "$temporary_directory/package-lock.json"
