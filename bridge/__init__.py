"""Matrix -> Telegram bridge (hexagonal architecture).

The `core` package holds pure domain logic with no third-party SDK imports.
Adapters (`matrix_source`, `telegram_sink`) plug into the core via the ports
defined in `core.ports`, so the core can be tested without any network.
"""

__version__ = "1.0.0"
