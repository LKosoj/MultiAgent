#!/bin/bash

# This script starts both the backend and frontend development servers.

# --- Backend ---
echo "Starting FastAPI backend server..."
export PATH="/Users/kosoj/Documents/MultiAgent/.venv/bin:$PATH"
./.venv/bin/python -m uvicorn backend.fastapi_app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
echo "Backend started with PID: $BACKEND_PID"

# --- Frontend ---
echo "Starting React frontend dev server..."
cd frontend/client
npm run dev &
FRONTEND_PID=$!
echo "Frontend started with PID: $FRONTEND_PID"
cd ../..

echo ""
echo "---"
echo "Application is starting."
echo "Backend API will be on http://localhost:8000"
echo "Frontend will be on http://localhost:5173"
echo ""
echo "To stop the servers, run the following command:"
echo "kill $BACKEND_PID $FRONTEND_PID"
echo "---"

# Wait for both processes to prevent the script from exiting immediately
wait $BACKEND_PID
wait $FRONTEND_PID
