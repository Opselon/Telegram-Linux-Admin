"""Infrastructure layer: adapters for sqlite, SSH and the local host.

Nothing here knows about Telegram or i18n; the application layer speaks to
these through the ports in :mod:`src.domain.ports`.
"""
