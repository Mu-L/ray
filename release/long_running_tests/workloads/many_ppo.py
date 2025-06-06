# This workload tests running many instances of PPO (many actors)
# This covers https://github.com/ray-project/ray/pull/12148

import ray
from ray.rllib.utils.metrics import NUM_ENV_STEPS_SAMPLED_LIFETIME
from ray.tune import run_experiments
from ray.tune.utils.release_test_util import ProgressCallback
from ray._private.test_utils import monitor_memory_usage

object_store_memory = 10**9
num_nodes = 3

message = (
    "Make sure there is enough memory on this machine to run this "
    "workload. We divide the system memory by 2 to provide a buffer."
)
assert (
    num_nodes * object_store_memory < ray._common.utils.get_system_memory() / 2
), message

# Simulate a cluster on one machine.

ray.init(address="auto")
monitor_actor = monitor_memory_usage()

# Run the workload.

run_experiments(
    {
        "ppo": {
            "run": "PPO",
            "env": "CartPole-v0",
            "num_samples": 10000,
            "config": {
                "num_env_runners": 7,
                "num_gpus": 0,
                "num_sgd_iter": 1,
            },
            "stop": {
                NUM_ENV_STEPS_SAMPLED_LIFETIME: 1,
            },
        }
    },
    callbacks=[ProgressCallback()],
)

ray.get(monitor_actor.stop_run.remote())
used_gb, usage = ray.get(monitor_actor.get_peak_memory_info.remote())
print(f"Peak memory usage: {round(used_gb, 2)}GB")
print(f"Peak memory usage per processes:\n {usage}")
