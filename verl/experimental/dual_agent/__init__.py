"""Experimental dual-agent PPO/GRPO training utilities.

The package intentionally avoids importing the trainer at module import time
because the trainer pulls in heavyweight distributed/model backends.  Import
``verl.experimental.dual_agent.trainer`` directly when launching training.
"""
