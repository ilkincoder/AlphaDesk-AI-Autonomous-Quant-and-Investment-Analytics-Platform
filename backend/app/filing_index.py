"""Indexing stored filing text into Qdrant, and retrieving passages from it.

PostgreSQL is authoritative. Qdrant is a derived index: everything here could be deleted and
rebuilt from `filing_documents` without losing a fact.

Two operations live here because they share the one thing that makes either safe — the
manifest, which says which documents are completely indexed and at what identity.

**Indexing** chunks each document, embeds the passages, writes them, and only then records the
document as complete. A run interrupted part-way leaves points in Qdrant and no manifest row,
which reads as "not indexed" and is the safe direction to be wrong in. A retry re-upserts the
same deterministic ids, so it converges rather than duplicating.

**Retrieval** filters inside Qdrant by company and by filing acceptance before choosing
results, then validates what comes back against the manifest. Anything indexed under a
different model, chunking version or source hash is dropped rather than shown, because a
passage whose vector no longer describes its text is a citation that cannot be trusted.

Nothing here computes a financial metric, invents a missing value, or recommends anything.
"""

import hashlib
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qdrant_client import models

from app.analysis import information_cutoff
from app.chunking import CHUNKING_VERSION, SectionBoundary, chunk_document
from app.embeddings import (
    EMBEDDING_MODEL,
    Embedder,
    EmbeddingUnavailableError,
)
from app.models import Company, DocumentIndexManifest, FilingDocument, SecFiling
from app.vector_store import (
    CollectionMismatchError,
    CollectionMissingError,
    Point,
    ScoredPoint,
    VectorStore,
    VectorStoreUnavailableError,
)

# The namespace every point id is derived in. Fixed forever: changing it would orphan every
# indexed point, because the ids would no longer match.
POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://alphadesk.local/sec-filings")

# How many candidates to ask for per wanted result. Validation drops hits whose document is
# incomplete or stale, and asking for exactly `top_k` would let those consume the whole result
# set. This multiple is the bound on the refill -- one query, with headroom, not a loop.
_OVERFETCH_FACTOR = 4
_MAX_QUERY_LIMIT = 100

# Retrieval statuses. Each is a different answer, and collapsing any two of them would make
# the caller report something that is not true.
STATUS_OK = "ok"
STATUS_UNKNOWN_COMPANY = "unknown_company"
STATUS_NO_INDEXED_DOCUMENTS = "no_indexed_documents"
STATUS_NO_ELIGIBLE_DOCUMENTS = "no_eligible_documents"
STATUS_NO_MATCHING_RESULTS = "no_matching_results"
STATUS_INDEX_UNAVAILABLE = "index_unavailable"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"

# The rows this module reads, as plain values rather than ORM objects. Everything downstream
# runs after the session that loaded them may have moved on, and a detached instance fails at
# an unpredictable moment rather than here.
@dataclass(frozen=True)
class _CompanyRow:
    id: int
    cik: str
    ticker: str


@dataclass(frozen=True)
class _FilingRow:
    accession_number: str
    form_type: str
    acceptance_datetime: datetime | None
    report_date: date | None


@dataclass(frozen=True)
class _DocumentRow:
    id: int
    document_name: str
    role: str
    source_url: str
    content_sha256: str
    extracted_text: str | None
    extraction_version: str | None
    sections: Mapping[str, Any] | None


# What happened to one document during an indexing run.
OUTCOME_INDEXED = "indexed"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_SKIPPED = "skipped"


class IndexingError(Exception):
    """Base class for every failure this module reports."""


class ReindexRequiredError(IndexingError):
    """Stored text or index configuration has changed under an existing index.

    Raised rather than resolved. Silently re-indexing would replace vectors that something may
    already be citing, and silently ignoring it would serve passages whose vectors describe
    text that is no longer there. Both are worse than stopping and saying so.
    """


@dataclass(frozen=True)
class DocumentOutcome:
    document_id: int
    document_name: str
    accession_number: str
    form_type: str
    status: str
    chunks: int
    reason: str | None = None


@dataclass(frozen=True)
class IndexSummary:
    symbol: str
    company_id: int | None
    collection_name: str
    collection_created: bool
    embedding_model: str
    chunking_version: str
    dry_run: bool
    documents: tuple[DocumentOutcome, ...]
    elapsed_seconds: float

    def counts(self) -> Mapping[str, int]:
        tally = {
            OUTCOME_INDEXED: 0,
            OUTCOME_UNCHANGED: 0,
            OUTCOME_SKIPPED: 0,
        }
        for outcome in self.documents:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    @property
    def chunk_count(self) -> int:
        return sum(outcome.chunks for outcome in self.documents)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "company_id": self.company_id,
            "collection": self.collection_name,
            "collection_created": self.collection_created,
            "embedding_model": self.embedding_model,
            "chunking_version": self.chunking_version,
            "dry_run": self.dry_run,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "documents": [
                {
                    "document_id": outcome.document_id,
                    "document_name": outcome.document_name,
                    "accession_number": outcome.accession_number,
                    "form_type": outcome.form_type,
                    "status": outcome.status,
                    "chunks": outcome.chunks,
                    "reason": outcome.reason,
                }
                for outcome in self.documents
            ],
            "counts": dict(self.counts()),
            "chunks": self.chunk_count,
        }


@dataclass(frozen=True)
class RetrievedPassage:
    """One passage, and everything needed to cite it back to the filing it came from."""

    text: str
    similarity: float
    section: str
    chunk_index: int
    start_offset: int
    end_offset: int
    company_id: int
    symbol: str
    cik: str
    accession_number: str
    form_type: str
    acceptance_datetime: datetime | None
    report_date: date | None
    document_id: int
    document_name: str
    document_role: str
    source_url: str
    content_sha256: str
    extracted_text_sha256: str
    extraction_version: str | None
    chunking_version: str
    embedding_model: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "similarity": self.similarity,
            "section": self.section,
            "chunk_index": self.chunk_index,
            "offsets": {"start": self.start_offset, "end": self.end_offset},
            "company": {"id": self.company_id, "symbol": self.symbol, "cik": self.cik},
            "filing": {
                "accession_number": self.accession_number,
                "form_type": self.form_type,
                "acceptance_datetime": _iso_dt(self.acceptance_datetime),
                "report_date": _iso(self.report_date),
                "source_url": self.source_url,
            },
            "document": {
                "id": self.document_id,
                "name": self.document_name,
                "role": self.document_role,
                "content_sha256": self.content_sha256,
                "extracted_text_sha256": self.extracted_text_sha256,
                "extraction_version": self.extraction_version,
                "chunking_version": self.chunking_version,
                "embedding_model": self.embedding_model,
            },
        }


@dataclass(frozen=True)
class RetrievalResult:
    """What a search found, or why it found nothing.

    `status` distinguishes the reasons, because "we hold no filings for that company" and "the
    index is down" call for entirely different responses from whoever reads it.
    """

    status: str
    query: str
    symbol: str
    as_of: date
    information_cutoff: datetime
    top_k: int
    passages: tuple[RetrievedPassage, ...]
    reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "query": self.query,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "information_cutoff": self.information_cutoff.isoformat(),
            "top_k": self.top_k,
            "returned": len(self.passages),
            "passages": [passage.as_dict() for passage in self.passages],
            "warnings": list(self.warnings),
        }


# --- indexing -------------------------------------------------------------------------------


def index_company(
    session: Session,
    *,
    symbol: str,
    store: VectorStore,
    embedder: Embedder,
    dry_run: bool = False,
) -> IndexSummary:
    """Index every eligible document of one company.

    Raises `ReindexRequiredError` when stored text or index configuration has changed under an
    existing index. Raises `EmbeddingUnavailableError` when the model cannot be used.
    """
    started = time.monotonic()
    company = _company(session, symbol)
    if company is None:
        return IndexSummary(
            symbol=symbol,
            company_id=None,
            collection_name=store.collection_name,
            collection_created=False,
            embedding_model=embedder.model_name,
            chunking_version=CHUNKING_VERSION,
            dry_run=dry_run,
            documents=(),
            elapsed_seconds=time.monotonic() - started,
        )

    rows = _indexable_documents(session, company.id)
    manifest = _manifest_for(session, [row.id for row, _ in rows])

    stale = _stale_documents(rows, manifest, store.collection_name, embedder.model_name)
    if stale:
        raise ReindexRequiredError(
            "the index no longer matches the stored text or the configuration, so these "
            "documents need reindexing before anything is served: "
            + "; ".join(stale)
            + ". Nothing was written. Drop the collection and index again -- this "
            "milestone has no partial-refresh path on purpose."
        )

    created = False
    if not dry_run:
        created = store.ensure_collection()

    outcomes: list[DocumentOutcome] = []
    for row, filing in rows:
        outcome = _index_document(
            session,
            row=row,
            filing=filing,
            company=company,
            manifest=manifest.get(row.id),
            store=store,
            embedder=embedder,
            collection_name=store.collection_name,
            dry_run=dry_run,
        )
        outcomes.append(outcome)

    return IndexSummary(
        symbol=company.ticker,
        company_id=company.id,
        collection_name=store.collection_name,
        collection_created=created,
        embedding_model=embedder.model_name,
        chunking_version=CHUNKING_VERSION,
        dry_run=dry_run,
        documents=tuple(outcomes),
        elapsed_seconds=time.monotonic() - started,
    )


def _index_document(
    session: Session,
    *,
    row: _DocumentRow,
    filing: _FilingRow,
    company: _CompanyRow,
    manifest: DocumentIndexManifest | None,
    store: VectorStore,
    embedder: Embedder,
    collection_name: str,
    dry_run: bool,
) -> DocumentOutcome:
    text = row.extracted_text or ""
    if not text.strip():
        # Skipped with a reason rather than indexed as nothing. A document with no readable
        # text is a fact about extraction, and an empty set of passages would hide it.
        return DocumentOutcome(
            document_id=row.id,
            document_name=row.document_name,
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            status=OUTCOME_SKIPPED,
            chunks=0,
            reason=(
                "no extracted text to index"
                if not text
                else "extracted text is blank"
            ),
        )

    text_hash = _sha256(text)
    identity = _identity(row, filing, text_hash, collection_name, embedder.model_name)

    if manifest is not None and _matches(manifest, identity):
        return DocumentOutcome(
            document_id=row.id,
            document_name=row.document_name,
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            status=OUTCOME_UNCHANGED,
            chunks=manifest.point_count,
        )

    chunks = chunk_document(
        text,
        form_type=filing.form_type,
        sections=_boundaries(row.sections),
        counter=embedder,
    )
    if dry_run:
        return DocumentOutcome(
            document_id=row.id,
            document_name=row.document_name,
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            status=OUTCOME_INDEXED,
            chunks=len(chunks),
            reason="dry run: nothing was written",
        )

    # Embedded in one batch per document. The vectors are produced before anything is written,
    # so a failure while embedding cannot leave a document half-written.
    vectors = embedder.embed_passages(chunk.embedding_input for chunk in chunks)
    points = [
        Point(
            id=_point_id(row.id, index),
            vector=vector,
            payload=_payload(
                company=company,
                filing=filing,
                row=row,
                chunk=chunk,
                chunk_index=index,
                text_hash=text_hash,
                embedding_model=embedder.model_name,
            ),
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]

    store.upsert(points)

    # Last, and only after every point was acknowledged. Its absence is what marks a document
    # as not completely indexed, so writing it earlier would be the one unrecoverable mistake.
    _record_manifest(session, row.id, collection_name, identity, len(points))

    return DocumentOutcome(
        document_id=row.id,
        document_name=row.document_name,
        accession_number=filing.accession_number,
        form_type=filing.form_type,
        status=OUTCOME_INDEXED,
        chunks=len(points),
    )


@dataclass(frozen=True)
class _Identity:
    collection_name: str
    embedding_model: str
    chunking_version: str
    content_sha256: str
    extracted_text_sha256: str
    extraction_version: str | None


def _identity(
    row: "_DocumentRow",
    filing: "_FilingRow",
    text_hash: str,
    collection_name: str,
    embedding_model: str,
) -> _Identity:
    return _Identity(
        collection_name=collection_name,
        embedding_model=embedding_model,
        chunking_version=CHUNKING_VERSION,
        content_sha256=row.content_sha256,
        extracted_text_sha256=text_hash,
        extraction_version=row.extraction_version,
    )


def _matches(manifest: DocumentIndexManifest, identity: _Identity) -> bool:
    return (
        manifest.collection_name == identity.collection_name
        and manifest.embedding_model == identity.embedding_model
        and manifest.chunking_version == identity.chunking_version
        and manifest.content_sha256 == identity.content_sha256
        and manifest.extracted_text_sha256 == identity.extracted_text_sha256
        and manifest.extraction_version == identity.extraction_version
    )


def _stale_documents(
    rows: Sequence[tuple["_DocumentRow", "_FilingRow"]],
    manifest: Mapping[int, DocumentIndexManifest],
    collection_name: str,
    model_name: str,
) -> list[str]:
    """Documents whose stored text or configuration no longer matches what was indexed."""
    stale: list[str] = []
    for row, filing in rows:
        existing = manifest.get(row.id)
        if existing is None:
            continue
        text_hash = _sha256(row.extracted_text or "")
        identity = _identity(row, filing, text_hash, collection_name, model_name)
        if not _matches(existing, identity):
            stale.append(f"{filing.accession_number}/{row.document_name}")
    return stale


def _record_manifest(
    session: Session,
    document_id: int,
    collection_name: str,
    identity: _Identity,
    point_count: int,
) -> None:
    session.add(
        DocumentIndexManifest(
            document_id=document_id,
            collection_name=collection_name,
            embedding_model=identity.embedding_model,
            chunking_version=identity.chunking_version,
            content_sha256=identity.content_sha256,
            extracted_text_sha256=identity.extracted_text_sha256,
            extraction_version=identity.extraction_version,
            point_count=point_count,
            indexed_at=datetime.now(timezone.utc),
        )
    )
    session.flush()


# --- retrieval ------------------------------------------------------------------------------


def search_filings(
    session: Session,
    *,
    symbol: str,
    query: str,
    as_of: date,
    top_k: int,
    store: VectorStore,
    embedder: Embedder,
) -> RetrievalResult:
    """Find passages relevant to `query`, as knowable at the end of `as_of`.

    Filters by company and filing acceptance inside Qdrant, then validates every hit against
    the manifest so a passage whose vector no longer describes its text is never shown.
    """
    cutoff = information_cutoff(as_of)

    def unavailable(status: str, reason: str, warnings: Sequence[str] = ()) -> RetrievalResult:
        return RetrievalResult(
            status=status,
            query=query,
            symbol=symbol,
            as_of=as_of,
            information_cutoff=cutoff,
            top_k=top_k,
            passages=(),
            reason=reason,
            warnings=tuple(warnings),
        )

    company = _company(session, symbol)
    if company is None:
        return unavailable(
            STATUS_UNKNOWN_COMPANY,
            f"no company is stored under {symbol!r}",
        )

    try:
        if not store.exists():
            return unavailable(
                STATUS_NO_INDEXED_DOCUMENTS,
                f"the {store.collection_name!r} collection does not exist, so nothing has "
                "been indexed",
            )
        store.validate_collection()
    except (VectorStoreUnavailableError, CollectionMissingError, CollectionMismatchError) as exc:
        return unavailable(STATUS_INDEX_UNAVAILABLE, str(exc))

    eligible = _eligible_documents(session, company.id, cutoff)
    if not eligible.manifests:
        return unavailable(
            STATUS_NO_INDEXED_DOCUMENTS,
            f"no document of {symbol} has been indexed",
        )
    if not eligible.eligible_document_ids:
        return unavailable(
            STATUS_NO_ELIGIBLE_DOCUMENTS,
            f"nothing indexed for {symbol} was accepted before {cutoff.isoformat()}",
            eligible.warnings,
        )

    try:
        vector = embedder.embed_query(query)
    except EmbeddingUnavailableError as exc:
        return unavailable(STATUS_MODEL_UNAVAILABLE, str(exc))

    limit = min(top_k * _OVERFETCH_FACTOR, _MAX_QUERY_LIMIT)
    try:
        hits = store.query(
            vector,
            query_filter=_cutoff_filter(company.id, cutoff),
            limit=limit,
        )
    except VectorStoreUnavailableError as exc:
        return unavailable(STATUS_INDEX_UNAVAILABLE, str(exc))

    warnings = list(eligible.warnings)
    validated, dropped = _validate(hits, eligible.manifests)
    # The query asked for more than were wanted so that validation could drop stale hits
    # without starving the result. What is returned is still what was asked for.
    passages = validated[:top_k]
    if dropped:
        warnings.append(
            f"{dropped} retrieved passage(s) were dropped because the document they came "
            "from is not completely indexed, or was indexed from text that has since "
            "changed. They are not shown, because their vectors may no longer describe "
            "their text."
        )
    if not passages:
        return RetrievalResult(
            status=STATUS_NO_MATCHING_RESULTS,
            query=query,
            symbol=symbol,
            as_of=as_of,
            information_cutoff=cutoff,
            top_k=top_k,
            passages=(),
            reason="no passage passed validation against the current index",
            warnings=tuple(warnings),
        )

    warnings.append(
        "These are passages retrieved by similarity. That they were returned does not mean "
        "the question is answered, and it says nothing about whether the filings held here "
        "are complete."
    )
    if len(passages) < top_k:
        warnings.append(
            f"{len(passages)} passage(s) were returned where {top_k} were asked for."
        )

    return RetrievalResult(
        status=STATUS_OK,
        query=query,
        symbol=symbol,
        as_of=as_of,
        information_cutoff=cutoff,
        top_k=top_k,
        passages=tuple(passages),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class _Eligibility:
    manifests: Mapping[int, DocumentIndexManifest]
    eligible_document_ids: frozenset[int]
    warnings: tuple[str, ...]


def _eligible_documents(
    session: Session, company_id: int, cutoff: datetime
) -> _Eligibility:
    """Indexed documents whose filing was publicly available before the cutoff.

    Availability is decided by the **acceptance timestamp** and nothing else. A filing's
    reporting-period date says which quarter it covers, and a retrieval timestamp says when
    this database was written -- neither says when the market could first have read it.
    """
    rows = session.execute(
        select(
            FilingDocument.id,
            SecFiling.acceptance_datetime,
            SecFiling.accession_number,
        )
        .join(SecFiling, SecFiling.id == FilingDocument.filing_id)
        .where(SecFiling.company_id == company_id)
    ).all()

    manifests = _manifest_for(session, [row.id for row in rows])
    if not manifests:
        return _Eligibility(manifests={}, eligible_document_ids=frozenset(), warnings=())

    eligible: set[int] = set()
    unknown_acceptance = 0
    for document_id, acceptance, _ in rows:
        if document_id not in manifests:
            continue
        if acceptance is None:
            unknown_acceptance += 1
            continue
        if acceptance < cutoff:
            eligible.add(document_id)

    warnings: list[str] = []
    if unknown_acceptance:
        warnings.append(
            f"{unknown_acceptance} indexed document(s) belong to filings with no acceptance "
            "timestamp, so they cannot be shown to have been public before the cutoff and "
            "are excluded."
        )
    return _Eligibility(
        manifests={key: value for key, value in manifests.items()},
        eligible_document_ids=frozenset(eligible),
        warnings=tuple(warnings),
    )


def _validate(
    hits: Sequence[ScoredPoint],
    manifests: Mapping[int, DocumentIndexManifest],
) -> tuple[list[RetrievedPassage], int]:
    """Keep only hits whose document is completely indexed at the identity it claims.

    A point can outlive its manifest row: an interrupted run leaves points behind, and a
    document removed from PostgreSQL leaves its vectors until something clears them. Both are
    caught here, because the payload is a claim and the manifest is the record.
    """
    passages: list[RetrievedPassage] = []
    seen: set[str] = set()
    dropped = 0

    for hit in hits:
        if hit.id in seen:
            continue
        payload = hit.payload
        document_id = payload.get("document_id")
        manifest = manifests.get(document_id) if isinstance(document_id, int) else None
        if manifest is None or not _payload_matches_manifest(payload, manifest):
            dropped += 1
            continue
        seen.add(hit.id)
        passages.append(_passage(hit, payload))

    return passages, dropped


def _payload_matches_manifest(
    payload: Mapping[str, Any], manifest: DocumentIndexManifest
) -> bool:
    return (
        payload.get("embedding_model") == manifest.embedding_model
        and payload.get("chunking_version") == manifest.chunking_version
        and payload.get("content_sha256") == manifest.content_sha256
        and payload.get("extracted_text_sha256") == manifest.extracted_text_sha256
    )


def _passage(hit: ScoredPoint, payload: Mapping[str, Any]) -> RetrievedPassage:
    return RetrievedPassage(
        text=str(payload.get("text", "")),
        # A similarity, never a percentage. Cosine similarity is not a probability and
        # presenting it as one invites a reading the number cannot support.
        similarity=hit.score,
        section=str(payload.get("section", "unknown")),
        chunk_index=int(payload.get("chunk_index", 0)),
        start_offset=int(payload.get("start_offset", 0)),
        end_offset=int(payload.get("end_offset", 0)),
        company_id=int(payload.get("company_id", 0)),
        symbol=str(payload.get("symbol", "")),
        cik=str(payload.get("cik", "")),
        accession_number=str(payload.get("accession_number", "")),
        form_type=str(payload.get("form_type", "")),
        acceptance_datetime=_parse_dt(payload.get("acceptance_datetime")),
        report_date=_parse_date(payload.get("report_date")),
        document_id=int(payload.get("document_id", 0)),
        document_name=str(payload.get("document_name", "")),
        document_role=str(payload.get("document_role", "")),
        source_url=str(payload.get("source_url", "")),
        content_sha256=str(payload.get("content_sha256", "")),
        extracted_text_sha256=str(payload.get("extracted_text_sha256", "")),
        extraction_version=payload.get("extraction_version"),
        chunking_version=str(payload.get("chunking_version", "")),
        embedding_model=str(payload.get("embedding_model", "")),
    )


# --- payload ----------------------------------------------------------------------------


def _payload(
    *,
    company: "_CompanyRow",
    filing: "_FilingRow",
    row: "_DocumentRow",
    chunk: Any,
    chunk_index: int,
    text_hash: str,
    embedding_model: str,
) -> dict[str, Any]:
    """Everything a citation needs, carried on the point itself.

    `acceptance_epoch` exists alongside the ISO string purely for filtering: Qdrant compares
    integers unambiguously, and a range filter on a formatted date would depend on the string
    parsing the same way on both sides.
    """
    acceptance = filing.acceptance_datetime
    return {
        "company_id": company.id,
        "cik": company.cik,
        "symbol": company.ticker,
        "accession_number": filing.accession_number,
        "form_type": filing.form_type,
        "acceptance_datetime": _iso_dt(acceptance),
        "acceptance_epoch": int(acceptance.timestamp()) if acceptance else None,
        "report_date": _iso(filing.report_date),
        "document_id": row.id,
        "document_name": row.document_name,
        "document_role": row.role,
        "source_url": row.source_url,
        "content_sha256": row.content_sha256,
        "extracted_text_sha256": text_hash,
        "extraction_version": row.extraction_version,
        "section": chunk.section,
        "chunk_index": chunk_index,
        "start_offset": chunk.start,
        "end_offset": chunk.end,
        "chunking_version": chunk.chunking_version,
        # The embedder's own name, not the module constant. Recording the constant
        # here would make every payload disagree with its manifest the moment the two
        # differed, and validation would drop every passage rather than none.
        "embedding_model": embedding_model,
        "token_count": chunk.token_count,
        "text": chunk.text,
    }


def _cutoff_filter(company_id: int, cutoff: datetime):
    """The filter Qdrant applies before it chooses anything.

    Two conditions, both necessary. Without the company match a search for one issuer would
    return another's filings; without the cutoff it would return documents that were not
    public on the date being asked about.

    A filing with no acceptance timestamp carries `acceptance_epoch: None`, which cannot
    satisfy a range condition, so it is excluded here rather than being silently included.
    """
    return models.Filter(
        must=[
            models.FieldCondition(
                key="company_id", match=models.MatchValue(value=company_id)
            ),
            models.FieldCondition(
                key="acceptance_epoch",
                range=models.Range(lt=int(cutoff.timestamp())),
            ),
        ]
    )


def _point_id(document_id: int, chunk_index: int) -> str:
    """A deterministic id, so a repeat run writes over its own points instead of beside them."""
    return str(
        uuid.uuid5(
            POINT_NAMESPACE,
            f"{document_id}:{CHUNKING_VERSION}:{EMBEDDING_MODEL}:{chunk_index}",
        )
    )


# --- reads ------------------------------------------------------------------------------


def _company(session: Session, symbol: str) -> _CompanyRow | None:
    row = session.execute(
        select(Company.id, Company.sec_issuer_cik, Company.ticker).where(
            func.upper(Company.ticker) == symbol.strip().upper()
        )
    ).one_or_none()
    return None if row is None else _CompanyRow(*row)


def _indexable_documents(
    session: Session, company_id: int
) -> list[tuple[_DocumentRow, _FilingRow]]:
    """Documents to consider, with the filing each belongs to.

    Every stored document is returned, including ones with no extracted text. Those are
    reported as skipped with a reason rather than filtered out here, because "we chose not to
    index this" and "it does not exist" are different answers.
    """
    rows = session.execute(
        select(
            FilingDocument.id,
            FilingDocument.document_name,
            FilingDocument.role,
            FilingDocument.source_url,
            FilingDocument.content_sha256,
            FilingDocument.extracted_text,
            FilingDocument.extraction_version,
            FilingDocument.sections,
            SecFiling.accession_number,
            SecFiling.form_type,
            SecFiling.acceptance_datetime,
            SecFiling.report_date,
        )
        .join(SecFiling, SecFiling.id == FilingDocument.filing_id)
        .where(SecFiling.company_id == company_id)
        .order_by(SecFiling.accession_number, FilingDocument.document_name)
    ).all()

    return [
        (
            _DocumentRow(
                id=row[0],
                document_name=row[1],
                role=row[2],
                source_url=row[3],
                content_sha256=row[4],
                extracted_text=row[5],
                extraction_version=row[6],
                sections=row[7],
            ),
            _FilingRow(
                accession_number=row[8],
                form_type=row[9],
                acceptance_datetime=row[10],
                report_date=row[11],
            ),
        )
        for row in rows
    ]


def _manifest_for(
    session: Session, document_ids: Sequence[int]
) -> dict[int, DocumentIndexManifest]:
    if not document_ids:
        return {}
    rows = session.scalars(
        select(DocumentIndexManifest).where(
            DocumentIndexManifest.document_id.in_(list(document_ids))
        )
    ).all()
    return {row.document_id: row for row in rows}


def _boundaries(sections: Mapping[str, Any] | None) -> list[SectionBoundary]:
    """The stored section map, as chunker boundaries.

    Stored as `{name: {"start": int, "heading": str}}`. Anything that does not look like that
    is ignored rather than guessed at, so a malformed map yields `unknown` sections instead
    of wrong ones.
    """
    if not isinstance(sections, Mapping):
        return []
    boundaries: list[SectionBoundary] = []
    for name, value in sections.items():
        if isinstance(value, Mapping) and isinstance(value.get("start"), int):
            boundaries.append(SectionBoundary(name=str(name), start=int(value["start"])))
    return boundaries


# --- small helpers ------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _iso_dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


