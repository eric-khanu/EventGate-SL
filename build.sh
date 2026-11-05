#!/bin/bash
set -e

echo "Installing requirements..."
pip install -r requirements.txt

# Since you compiled output.css locally and pushed it, you don't need Node.js on Vercel.
# Just collect the static files.
echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Running database migrations..."
python manage.py migrate --noinput