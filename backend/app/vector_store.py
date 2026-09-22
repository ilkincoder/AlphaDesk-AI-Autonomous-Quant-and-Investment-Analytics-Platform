"""The Qdrant collection: creating it, checking it, writing to it, querying it.

Deliberately payload-agnostic. It knows about vectors, ids and filters; it knows nothing about
filings, companies or acceptance timestamps. Those belong to `app.filing_index`, which is why
this module can be tested against an in-memory Qdrant with arbitrary payloads.

**Three rules this module enforces, all of which are easy to get wrong.**

* **The collection is validated, never assumed.** Size and distance are read back from Qdrant
  and compared. A collection built for a different model would accept these vectors and return
  nonsense, so the mismatch is caught before a single point is written.
* **An existing collection is never recreated.** Indexing creates one when it is absent and
  says so; nothing here drops anything. Losing an index should be a decision, not a side
  effect of running a command twice.
* **`query_points`, not `search`.** The `search` method was removed from qdrant-client by
  1.19; a good deal of documentation still shows it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient, models

# What the embedding model produces. Checked against the collection, never assumed.
VECTOR_SIZE = 384
DISTANCE = models.Distance.COSINE

_CLIENT_TIMEOUT_SECONDS = 10


class VectorStoreError(Exception):
    """Base class for every failure this module reports."""


class VectorStoreUnavailableError(VectorStoreError):
    """Qdrant could not be reached.

    Its own type because "the search index is down" and "the search found nothing" are
    different answers, and a caller that cannot tell them apart reports the wrong one.
    """


class CollectionMissingError(VectorStoreError):
    """The collection does not exist, so there is nothing indexed to search."""


class CollectionMismatchError(VectorStoreError):
    """The collection exists but is not configured the way this code requires."""


@dataclass(frozen=True)
class Point:
    """One vector, its deterministic id, and the payload that cites it."""

    id: str
    vector: Sequence[float]
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredPoint:
    """A query result: the id, the similarity, and the citation carried with it."""

    id: str
    score: float
    payload: Mapping[str, Any]


class VectorStore:
    """A handle on one Qdrant collection.

    The client is created on first use, so importing this module — or starting the API —
    connects to nothing.
    """

    def __init__(
        self,
        *,
        url: str,
        collection_name: str,
        client: QdrantClient | None = None,
    ) -> None:
        self._url = url
        self._collection = collection_name
        self._client = client

    @property
    def collection_name(self) -> str:
        return self._collection

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            try:
                self._client = QdrantClient(
                    url=self._url, timeout=_CLIENT_TIMEOUT_SECONDS
                )
            except Exception as exc:
                raise VectorStoreUnavailableError(
                    f"could not create a Qdrant client for {self._url}: "
                    f"{type(exc).__name__}"
                ) from exc
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # --- the collection ------------------------------------------------------------------

    def ensure_collection(self) -> bool:
        """Create the collection if it is absent, and validate it if it is not.

        Returns True when it created one, so the caller can say so. An existing collection is
        validated and otherwise left completely alone.
        """
        if self.exists():
            self.validate_collection()
            return False

        try:
            self.client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=DISTANCE),
            )
        except Exception as exc:
            raise VectorStoreError(
                f"could not create the {self._collection!r} collection: "
                f"{type(exc).__name__}"
            ) from exc
        return True

    def exists(self) -> bool:
        try:
            return bool(self.client.collection_exists(self._collection))
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"could not reach Qdrant at {self._url} to check for the "
                f"{self._collection!r} collection: {type(exc).__name__}"
            ) from exc

    def validate_collection(self) -> None:
        """Refuse a collection that is missing or not built for these vectors."""
        try:
            info = self.client.get_collection(self._collection)
        except Exception as exc:
            raise CollectionMissingError(
                f"the {self._collection!r} collection could not be read: "
                f"{type(exc).__name__}"
            ) from exc

        vectors = info.config.params.vectors
        if not isinstance(vectors, models.VectorParams):
            # A named-vector collection. This code writes one unnamed vector per point, so
            # anything else would be a different shape of index entirely.
            raise CollectionMismatchError(
                f"the {self._collection!r} collection uses multiple named vectors; this "
                f"code writes a single unnamed {VECTOR_SIZE}-dimensional vector"
            )

        if vectors.size != VECTOR_SIZE or vectors.distance != DISTANCE:
            raise CollectionMismatchError(
                f"the {self._collection!r} collection holds {vectors.size}-dimensional "
                f"vectors with {vectors.distance}, but this code requires {VECTOR_SIZE} "
                f"with {DISTANCE}. Refusing to mix incompatible vectors: drop the "
                "collection and index again."
            )

    # --- writing -------------------------------------------------------------------------

    def upsert(self, points: Sequence[Point]) -> int:
        """Write points, acknowledged before returning.

        `wait=True` is what makes "all points acknowledged" a real statement rather than an
        optimistic one: without it Qdrant accepts the write and applies it later, and a
        manifest row written on that basis would be claiming something that had not happened.
        """
        if not points:
            return 0
        try:
            self.client.upsert(
                collection_name=self._collection,
                points=[
                    models.PointStruct(
                        id=point.id,
                        vector=list(point.vector),
                        payload=dict(point.payload),
                    )
                    for point in points
                ],
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(
                f"could not write {len(points)} point(s) to {self._collection!r}: "
                f"{type(exc).__name__}"
            ) from exc
        return len(points)

    def delete(self, *, query_filter: models.Filter) -> None:
        """Remove every point matching `query_filter`, acknowledged before returning.

        For replacing a document's chunks when its text changes. Deleting by *filter* rather
        than by a list of ids is what makes it complete: the ids of the previous version are
        not something the caller has to have kept, and a version that produced more chunks
        than the one replacing it would otherwise leave its tail behind forever.

        `wait=True` for the same reason `upsert` uses it -- a caller that writes new points
        immediately afterwards must not race the delete it depends on.
        """
        try:
            self.client.delete(
                collection_name=self._collection,
                points_selector=models.FilterSelector(filter=query_filter),
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(
                f"could not delete points from {self._collection!r}: "
                f"{type(exc).__name__}"
            ) from exc

    # --- reading -------------------------------------------------------------------------

    def query(
        self,
        vector: Sequence[float],
        *,
        query_filter: models.Filter | None = None,
        limit: int,
    ) -> list[ScoredPoint]:
        """The `limit` nearest points, filtered inside Qdrant.

        The filter is applied by Qdrant before it chooses results -- not afterwards here --
        so an excluded filing cannot occupy one of the `limit` places.
        """
        try:
            response = self.client.query_points(
                collection_name=self._collection,
                query=list(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"could not query {self._collection!r}: {type(exc).__name__}"
            ) from exc

        return [
            ScoredPoint(
                id=str(point.id),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in response.points
        ]

    def count(self, *, query_filter: models.Filter | None = None) -> int:
        try:
            result = self.client.count(
                collection_name=self._collection,
                count_filter=query_filter,
                exact=True,
            )
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"could not count points in {self._collection!r}: {type(exc).__name__}"
            ) from exc
        return int(result.count)
