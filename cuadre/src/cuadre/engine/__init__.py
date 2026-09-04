"""The five-stage reconciliation engine.

Stages 2, 3 and 5 are pure functions over the dataclasses in `types`: no
session, no clock, no network. The same inputs give the same output, which is
what makes a period replayable and a workpaper defensible.
"""
