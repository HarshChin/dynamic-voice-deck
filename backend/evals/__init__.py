"""Agent evaluations (TRD §13).

Unit tests check the code; these check the *agent*, whose behaviour is a property of a model and
therefore not deterministic. They call real providers, cost free-tier quota, and are run at
milestone boundaries rather than on every commit.
"""
