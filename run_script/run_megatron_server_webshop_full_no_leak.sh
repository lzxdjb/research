#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  start_webshop_servers.sh
#  Launches NUM_SERVERS parallel WebShop FastAPI instances, one per port.
#  Mirrors the ALFWorld launch script exactly.
# ─────────────────────────────────────────────────────────────────────────────

pkill -f "webshop_server_no_leak" 2>/dev/null || true

cd /cpfs01/nlp/leizhengxing/stock-rl/webshop
source /cpfs01/nlp/leizhengxing/stock-rl/miniconda3/bin/activate
conda activate webshop

PREFERRED_PORT=9200
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
WEBSHOP_NUM_PRODUCTS="all"  
WEBSHOP_POOL_SIZE=50
HOST_IP=$(hostname -I | awk '{print $1}')

echo "Starting $NUM_SERVERS WebShop servers from port $BASE_PORT on $HOST_IP ..."

for i in $(seq 0 $((NUM_SERVERS - 1))); do
    PORT=$((BASE_PORT + i))
    PORT=$PORT \
    WEBSHOP_OBS_MODE=text \
    WEBSHOP_NUM_PRODUCTS=$WEBSHOP_NUM_PRODUCTS \
    uvicorn webshop_server_no_leak:app \
        --host 0.0.0.0 \
        --port $PORT \
        --workers 1 &
done

echo "Waiting for all $NUM_SERVERS servers to be ready ..."
READY=0
while [ $READY -lt $NUM_SERVERS ]; do
    READY=0
    for i in $(seq 0 $((NUM_SERVERS - 1))); do
        PORT=$((BASE_PORT + i))
        if curl -sf http://localhost:$PORT/health > /dev/null 2>&1; then
            READY=$((READY + 1))
        fi
    done
    echo "  $READY / $NUM_SERVERS ready ..."
    sleep 5
done

echo "All $NUM_SERVERS WebShop servers ready."
echo "WEBSHOP_SERVER_HOST=$HOST_IP"
echo "WEBSHOP_BASE_PORT=$BASE_PORT"
echo "WEBSHOP_NUM_SERVERS=$NUM_SERVERS"