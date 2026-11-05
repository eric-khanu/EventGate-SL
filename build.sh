#!/bin/bash
set -e

# Install the system library for python-magic
echo "Installing libmagic..."
apt-get update && apt-get install -y libmagic1

# Install Python dependencies
echo "Installing requirements..."
pip install -r requirements.txt

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Run database migrations
echo "Running database migrations..."
python manage.py migrate --noinput