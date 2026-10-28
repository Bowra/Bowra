"""Channel-neutral IM layer used by the Feishu integration.

The `im` package holds the provider-neutral pieces shared by every IM channel:
the `im_*` event store (this module's `store.py`), and — added in later P0/P1 phases —
the neutral block model + event->block render machine used by the supported provider bridge.
"""
