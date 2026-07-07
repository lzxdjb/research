#!/bin/bash
pkill -f uvicorn
cd /cpfs01/nlp/leizhengxing/stock-rl
source /cpfs01/nlp/leizhengxing/stock-rl/miniconda3/bin/activate
conda activate alfworld
export ALFWORLD_DATA=/cpfs01/nlp/leizhengxing/stock-rl/data/alfworld_env_data/alfworld

PREFERRED_PORT=8700
NUM_SERVERS=1

find_free_port() {
    local port=$1
    while (echo >/dev/tcp/localhost/$port) 2>/dev/null; do
        port=$((port + 1))
    done
    echo $port
}

BASE_PORT=$(find_free_port $PREFERRED_PORT)
if [ "$BASE_PORT" != "$PREFERRED_PORT" ]; then
    echo "Port $PREFERRED_PORT in use, using $BASE_PORT instead."
fi
ALFWORLD_POOL_SIZE=300
HOST_IP=$(hostname -I | awk '{print $1}')  # or hardcode your machine B IP

echo "Starting $NUM_SERVERS ALFWorld servers from port $BASE_PORT on $HOST_IP..."

for i in $(seq 0 $((NUM_SERVERS - 1))); do
    PORT=$((BASE_PORT + i))
    PORT=$PORT uvicorn alfworld_server_no_leak:app \
        --host 0.0.0.0 \
        --port $PORT \
        --workers 1 &
done

echo "Waiting for all $NUM_SERVERS servers to be ready..."
READY=0
while [ $READY -lt $NUM_SERVERS ]; do
    READY=0
    for i in $(seq 0 $((NUM_SERVERS - 1))); do
        PORT=$((BASE_PORT + i))
        if curl -sf http://localhost:$PORT/health > /dev/null 2>&1; then
            READY=$((READY + 1))
        fi
    done
    echo "$READY / $NUM_SERVERS ready..."
    sleep 5
done

echo "All servers ready."
echo "ALFWORLD_SERVER_HOST=$HOST_IP"
echo "ALFWORLD_BASE_PORT=$BASE_PORT"
echo "ALFWORLD_NUM_SERVERS=$NUM_SERVERS"
echo "ALFWORLD_POOL_SIZE=$ALFWORLD_POOL_SIZE"
wait
