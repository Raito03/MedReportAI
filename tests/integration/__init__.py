"""P0-T5 integration test package — Mode B (explicit OpenRouter tests).

Nothing in this directory runs during a normal `pytest` run:
each test is gated behind the OPENROUTER_RUN_INTEGRATION=1 environment
variable and additionally skips when OPENROUTER_API_KEY is not configured.
"""
