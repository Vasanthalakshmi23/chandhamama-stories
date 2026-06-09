#!/bin/bash
# Start the Chandamama Kathalu API server
# Requires: PostgreSQL running on port 5433 at /home/surya/pg_data

PORT=${1:-8084}
cd "$(dirname "$0")"
echo "Starting Chandamama Kathalu API on port $PORT..."
nohup python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" > /tmp/chandamama_server.log 2>&1 &
echo "PID: $!"
echo "Server log: /tmp/chandamama_server.log"
echo "API: http://localhost:$PORT"
echo "Frontend: http://localhost:$PORT/"
