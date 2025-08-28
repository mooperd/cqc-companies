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
    cat > .env << EOF
DATABASE_URL=postgresql://postgres:password@localhost:5432/crm_db
EOF
fi

# Start the application
echo "Starting Flask application..."
echo "Access the app at: http://localhost:5000"
python app.py