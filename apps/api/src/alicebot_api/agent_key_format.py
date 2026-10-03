"""The shape of an issued agent API key, with no imports.

A leaf module so the credential floor can recognise the product's own key
without importing vnext_agent_keys, which imports the promotion policy,
which calls the credential floor. vnext_agent_keys re-exports both names.
"""

from __future__ import annotations

AGENT_KEY_PREFIX = "alice_sk_"
AGENT_KEY_PREFIX_LENGTH = 12

__all__ = ["AGENT_KEY_PREFIX", "AGENT_KEY_PREFIX_LENGTH"]
