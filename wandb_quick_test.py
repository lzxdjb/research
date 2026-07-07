import os
import multiprocessing as mp
import wandb

def worker():
    print("Worker WANDB_MODE:", os.environ.get("WANDB_MODE"))

    run = wandb.init(
        project="test",
        name="worker",
        settings=wandb.Settings(init_timeout=1200),
    )
    run.finish()
    print("Worker success")

if __name__ == "__main__":
    # os.environ["WANDB_MODE"] = "offline"

    p = mp.Process(target=worker)
    p.start()
    p.join()