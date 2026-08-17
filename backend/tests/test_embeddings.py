from __future__ import annotations

from app.services.embeddings import ONLINE_EMBEDDING_BATCH_SIZE, OnlineEmbeddings


class FakeOpenAIEmbeddings:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [[float(text)] for text in texts]


def test_online_embeddings_batches_at_provider_limit() -> None:
    provider = OnlineEmbeddings.__new__(OnlineEmbeddings)
    client = FakeOpenAIEmbeddings()
    provider._client = client  # type: ignore[assignment]
    texts = [str(index) for index in range(ONLINE_EMBEDDING_BATCH_SIZE * 2 + 2)]

    embeddings = provider.embed_documents(texts)

    assert client.batch_sizes == [64, 64, 2]
    assert embeddings == [[float(text)] for text in texts]
