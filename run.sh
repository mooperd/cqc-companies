#!/bin/bash

# Simple CRM App Launcher
echo "Starting Simple CRM App..."

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install requirements
echo "Installing requirements..."
pip install -r requirements.txt

# Set environment variables if .env doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    # Generate a unique local SECRET_KEY so the app doesn't fall back to an
    # ephemeral one (new key every restart). Rotate before any shared/prod use.
    secret_key=$(python -c 'import secrets; print(secrets.token_hex(32))')
    cat > .env << EOF
SECRET_KEY=$secret_key
DATABASE_URL=postgresql://postgres:password@localhost:5432/crm_db
EOF
fi

# Start the application
echo "Starting Flask application..."
echo "Access the app at: http://localhost:5000"
python app.py