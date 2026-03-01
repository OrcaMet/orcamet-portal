#!/usr/bin/env bash
# OrcaMet Portal — Render Build Script
# Runs on every deploy

set -o errexit

echo "=== Installing dependencies ==="
pip install --no-cache-dir numpy pandas
pip install --no-cache-dir -r requirements.txt

echo "=== Collecting static files ==="
python manage.py collectstatic --no-input

echo "=== Running database migrations ==="
python manage.py migrate

echo "=== Build complete ==="
