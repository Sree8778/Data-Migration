"""Vector pre-filter: embeds the SAP target columns and retrieves top-k candidates.

Embeddings come from a small local sentence-transformers model (default
BAAI/bge-small-en-v1.5, ~130 MB) and are cached on disk, keyed by a hash of the model name
and the exact documents, so a catalog change automatically invalidates the cache.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Protocol

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from src.db.enums import TargetType
from src.db.models import CatColumn, CatDataSource, CatTable
from src.schemas.mapping_ai import TargetCandidate

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


GENERATED_KEYS = ("PARTNER", "KUNNR", "LIFNR", "ADDRNUMBER")

class Encoder(Protocol):
    """Anything that turns texts into L2-normalised vectors (rows)."""

    name: str

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray: ...


class SentenceTransformerEncoder:
    """Lazy wrapper: the model (and torch) are only loaded on first use."""

    def __init__(self, model_name: str = DEFAULT_MODEL, threads: int = 2) -> None:
        self.name = model_name
        self._threads = threads
        self._model = None

    def _load(self):
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(self._threads)
            self._model = SentenceTransformer(self.name, device="cpu")
        return self._model

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        if is_query and "bge" in self.name.lower():
            texts = [_BGE_QUERY_PREFIX + t for t in texts]
        vectors = self._load().encode(
            texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
        )
        return np.asarray(vectors, dtype=np.float32)


@dataclass(frozen=True)
class _IndexedColumn:
    column_id: int
    table_id: int
    table_name: str
    column_name: str
    data_type: str
    char_max_length: Optional[int]
    business_name: Optional[str]
    description: Optional[str]
    check_table: Optional[str]
    is_mandatory: bool
    allowed_values: tuple[str, ...]

    def document(self) -> str:
        """Text that gets embedded: readable column name + SAP description.

        Technical type/length are deliberately left out: in our evaluation they lowered top-1
        retrieval accuracy (12/18 vs 16/18). They reach the LLM through the prompt instead.
        """
        name = normalise_identifier(self.column_name)
        return f"{name}: {self.description}" if self.description else name


def normalise_identifier(name: str) -> str:
    """CUST_NAME / custName / cust-name -> 'cust name'."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return re.sub(r"[_\-\.\s]+", " ", spaced).strip().lower()


# Legacy column names are written in abbreviations a language model does not connect to the words in a field's description ("cust" is not "customer" to it). The words are
# added to the query beside the original, never instead of it.
ABBREVIATIONS: dict[str, str] = {
    "cust": "customer", "custno": "customer number", "kd": "customer", "kunde": "customer", "no": "number", "nr": "number", "num": "number", "nbr": "number", "id": "identifier number",
    "addr": "address", "adr": "address", "strasse": "street", "str": "street", "strt": "street", "tel": "telephone", "phn": "phone", "ph": "phone", "mob": "mobile phone",
    "ctry": "country", "cntry": "country", "cty": "city", "zip": "postal code", "pstl": "postal", "plz": "postal code", "nm": "name", "nme": "name", "curr": "currency", "ccy": "currency",
    "pmt": "payment", "pay": "payment", "trm": "terms", "lang": "language", "lng": "language", "eml": "e-mail", "acct": "account", "acc": "account", "grp": "group", "cd": "code",
    "typ": "type", "vend": "vendor supplier", "sup": "supplier", "vat": "tax number vat", "tin": "tax number", "ein": "tax number", "dt": "date", "desc": "description",
    # German SAP-style names: a model that reads English descriptions does not connect "FIRMENNAME" to the organization name, or "UST_IDNR" to the VAT number
    "firmenname": "company name organization name", "firma": "company organization name", "kundenname": "customer name organization name", "name1": "organization name 1",
    "zusatz": "second name line additional name", "namenszusatz": "second name line additional name", "name2": "organization name 2 second name line",
    "ust": "vat tax number", "idnr": "identification number", "ustid": "vat tax number", "ustidnr": "vat identification number tax number", "steuernummer": "tax number",
    "steuer": "tax", "kundennummer": "customer number", "debitor": "customer", "hausnr": "house number", "hausnummer": "house number", "ort": "city", "stadt": "city",
    "land": "country", "postleitzahl": "postal code", "mail": "e-mail address", "telefon": "telephone number", "sprache": "language", "waehrung": "currency",
    "zahlungsbed": "payment terms", "zahlungsbedingung": "payment terms", "zahlungsbedingungen": "payment terms",
}


def expand_abbreviations(query: str) -> str:
    """'cust id' -> 'cust id customer identifier number': the words behind the abbreviations, appended."""
    extra = [ABBREVIATIONS[t] for t in query.split() if t in ABBREVIATIONS]
    return query + (" " + " ".join(dict.fromkeys(extra)) if extra else "")


class EmbeddingService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        encoder: Optional[Encoder] = None,
        cache_dir: Optional[str | Path] = None,
        object_name: str = "BUSINESS_PARTNER",
    ) -> None:
        self._session_factory = session_factory
        self._encoder: Encoder = encoder or SentenceTransformerEncoder()
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._object_name = object_name
        self._columns: list[_IndexedColumn] = []
        self._matrix: Optional[np.ndarray] = None

    @property
    def object_name(self) -> str:
        return self._object_name

    # ------------------------------------------------------------------ index
    def build_index(self, force: bool = False) -> int:
        """(Re)load target columns from the catalog and embed them. Returns the column count."""
        columns = self._load_target_columns()
        if not columns:
            raise LookupError(f"no target columns registered for object '{self._object_name}'")
        docs = [c.document() for c in columns]
        matrix = None if force else self._read_cache(docs)
        if matrix is None:
            matrix = self._encoder.encode(docs)
            self._write_cache(docs, matrix)
        self._columns, self._matrix = columns, matrix
        return len(columns)

    def _load_target_columns(self) -> list[_IndexedColumn]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(CatColumn)
                .join(CatTable, CatColumn.table_id == CatTable.id)
                .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(
                    CatDataSource.target_type == TargetType.TARGET,
                    CatTable.business_object == self._object_name,
                    CatColumn.column_name.not_in(GENERATED_KEYS),  # numbers SAP assigns are never mapped; they only crowd out the real candidates
                )
                .options(joinedload(CatColumn.table))
                .order_by(CatTable.id, CatColumn.ordinal_position, CatColumn.id)
            ).all()
            return [
                _IndexedColumn(
                    column_id=c.id,
                    table_id=c.table_id,
                    table_name=c.table.table_name,
                    column_name=c.column_name,
                    data_type=c.data_type,
                    char_max_length=c.char_max_length,
                    business_name=c.table.business_name,
                    description=c.description,
                    check_table=c.check_table,
                    is_mandatory=c.is_mandatory,
                    allowed_values=tuple(str(v) for v in (c.sample_values or [])[:8]),
                )
                for c in rows
            ]

    def _cache_path(self, docs: list[str]) -> Optional[Path]:
        if self._cache_dir is None:
            return None
        digest = hashlib.sha256(json.dumps([self._encoder.name, docs]).encode()).hexdigest()[:24]
        return self._cache_dir / f"target_embeddings_{digest}.npy"

    def _read_cache(self, docs: list[str]) -> Optional[np.ndarray]:
        path = self._cache_path(docs)
        if path is not None and path.is_file():
            matrix = np.load(path)
            if matrix.shape[0] == len(docs):
                return matrix
        return None

    def _write_cache(self, docs: list[str], matrix: np.ndarray) -> None:
        path = self._cache_path(docs)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.save(path, matrix)

    # ------------------------------------------------------------------ retrieval
    def get_top_k_candidates(
        self, source_column_name: str, source_data_type: str, top_k: int = 5, sample_values: Optional[list[str]] = None, hint: Optional[str] = None,
        exclude_tables: Optional[Iterable[str]] = None, must_include: Optional[Iterable[tuple[str, str]]] = None,
    ) -> list[TargetCandidate]:
        """Top-k target columns by cosine similarity (scores in [-1, 1], best first).

        Cryptic legacy names (ORT01, PSTLZ, LAND1) mean nothing to an embedding model, so a few sample values
        are appended to the query ("ort01, e.g. Berlin, Dresden"). They are embedded locally and never leave
        the machine; values the PII policy masks arrive here already masked.

        `hint` is a person's note about the column ("this is the customer account group"). A model can only choose among the candidates it is shown, so the hint
        must steer the RETRIEVAL as well as the prompt: the hint alone and the name with the hint are ranked too, and their best matches come first.

        `exclude_tables`: target tables that are not offered at all, for example the supplier tables when the source is a customer extract: a field that does not apply is
        not a candidate, so the model cannot pick it however similar its description is.

        `must_include`: (table, column) targets that are always among the candidates, in the last place if the ranking did not reach them: for a column the application can tell
        is the customer key, the field the key belongs in must be offered however the similarity falls out."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if self._matrix is None:
            self.build_index()
        # `source_data_type` is not embedded (see _IndexedColumn.document); it is part of the
        # LLM prompt, where type compatibility is judged.
        query = expand_abbreviations(normalise_identifier(source_column_name))
        samples = [str(v).strip() for v in (sample_values or []) if str(v).strip()][:5]

        excluded = {t.upper() for t in (exclude_tables or ())}
        allowed = [i for i, c in enumerate(self._columns) if c.table_name.upper() not in excluded]

        def ranking(text: str) -> tuple[list[int], np.ndarray]:
            scores = self._matrix @ self._encoder.encode([text], is_query=True)[0]  # rows are unit-length -> dot == cosine
            # Stable ordering: score desc, then catalog order, so results are reproducible.
            return sorted(allowed, key=lambda i: (-float(scores[i]), i)), scores

        by_name, scores = ranking(query)
        order = by_name[:top_k]
        rankings = [by_name]
        hint = (hint or "").strip()[:300]
        if samples:  # interleave the name-only ranking with the sample-aware one: samples rescue cryptic names, names rescue odd samples
            by_samples, sample_scores = ranking(f"{query}, e.g. {', '.join(samples)}")
            scores = np.maximum(scores, sample_scores)
            rankings.append(by_samples)
        if hint:  # the person's own words come first
            by_hint, hint_scores = ranking(hint)
            by_name_hint, name_hint_scores = ranking(f"{query}, {hint}")
            scores = np.maximum(scores, np.maximum(hint_scores, name_hint_scores))
            rankings = [by_hint, by_name_hint, *rankings]
        if len(rankings) > 1:
            order = []
            for position in range(len(by_name)):
                for ranked in rankings:
                    if ranked[position] not in order:
                        order.append(ranked[position])
                if len(order) >= top_k:
                    break
            order = order[:top_k]
        wanted = {(t.upper(), c.upper()) for t, c in (must_include or ())}
        for i in allowed:                                       # pinned targets the similarity did not reach take the last place
            c = self._columns[i]
            if (c.table_name.upper(), c.column_name.upper()) in wanted and i not in order:
                order = order[:max(0, top_k - 1)] + [i]
        return [
            TargetCandidate(
                column_id=c.column_id,
                table_id=c.table_id,
                table_name=c.table_name,
                column_name=c.column_name,
                data_type=c.data_type,
                char_max_length=c.char_max_length,
                business_name=c.business_name,
                description=c.description,
                check_table=c.check_table,
                is_mandatory=c.is_mandatory,
                score=round(max(-1.0, min(1.0, float(scores[i]))), 4),
            )
            for i in order
            for c in [self._columns[i]]
        ]
