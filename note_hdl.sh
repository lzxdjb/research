kill "$(cat formal_run_log/burn_gpu_smart.pid)"
rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/rollout_log/hdl_agent_smoke
VERIFY_HDL_ENV=0 TRAIN_BATCH_SIZE=16 PPO_MINI_BATCH_SIZE=16 ROLLOUT_N=1 TP_SIZE=4 N_GPUS_PER_NODE=8 TOTAL_EPOCHS=1 bash run_script/reflect_demo_hdl.sh
python3 burn_gpu_smart.py &
nohup bash run_script/start_digital_onboarding_4b_multirole_server.sh \
  > /tmp/digital_onboarding_4b_server.log 2>&1 &

echo $! > /tmp/digital_onboarding_4b_server.pid
disown
ps -eo pid,ppid,stat,pcpu,comm,args --sort=-pcpu | grep 'multiprocessing.spawn'

  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
  ray stop
pgrep -f 'multiprocessing\.spawn.*spawn_main' | xargs -r kill -KILL