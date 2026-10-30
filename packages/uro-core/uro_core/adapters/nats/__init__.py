"""NATS JetStream outbox publisher (D-57) — install with `uro-core[nats]`. A concrete adapter: the
core ring never imports it (import-linter)."""

from uro_core.adapters.nats.publisher import NatsJetStreamPublisher

__all__ = ["NatsJetStreamPublisher"]
