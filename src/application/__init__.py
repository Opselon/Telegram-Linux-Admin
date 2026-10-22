"""Application layer: use cases that orchestrate domain + infrastructure.

The layer is transport and UI agnostic.  Handlers in
:mod:`src.presentation.telegram` call these use cases; they never touch
sqlite, asyncssh or subprocesses directly.
"""
