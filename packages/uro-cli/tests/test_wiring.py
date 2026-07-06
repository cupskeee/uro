"""CLI wiring: the embedder role must get an embedding model, not the chat model."""

from uro_cli.wiring import build_embedder, build_router


def test_stub_needs_no_separate_embedder() -> None:
    assert build_embedder("stub") is None  # StubProvider.embed works


def test_real_providers_bind_a_separate_embedder() -> None:
    # Real providers distinguish chat vs embedding endpoints — a separate binding is
    # required so the embedder role does not POST a chat model to /embeddings (review 1.3).
    local = build_embedder("local")
    assert local is not None and "embedder" in build_router("local", None)._bindings


async def test_stub_router_embeds_via_default() -> None:
    router = build_router("stub", None)
    vectors = await router.embed("embedder", ["hello world"])
    assert len(vectors) == 1 and len(vectors[0]) == 256
