#!/usr/bin/env bash
set -o errexit

echo "=== Installing dependencies ==="
pip install --no-cache-dir -r requirements.txt

echo "=== Collecting static files ==="
python manage.py collectstatic --no-input --verbosity 2

echo "=== Running database migrations ==="
python manage.py migrate --verbosity 2

echo "=== Checking templates ==="
python manage.py check --deploy

echo "=== Build complete ==="
