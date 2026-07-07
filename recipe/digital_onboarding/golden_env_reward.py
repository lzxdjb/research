"""Golden environment reward for the service model.

This is intentionally not an LLM reward model. The reward is computed from the
deterministic onboarding tool environment: authentication state, collected KYC
fields, document state, errors, and final application-submission state.
"""

from recipe.digital_onboarding.reward_function import compute_score

__all__ = ["compute_score"]
