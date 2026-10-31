"""Pydantic v2 models for Module 4: lookups, transformation, deduplication, run reporting."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ lookups
class LookupFallback(str, enum.Enum):
    PASS_THROUGH = "PASS_THROUGH"  # unmapped value is carried over unchanged
    FLAG_FAILED = "FLAG_FAILED"  # value becomes NULL and the cell is flagged LOOKUP_FAILED
    USE_DEFAULT = "USE_DEFAULT"  # unmapped value is replaced by a configured default


class LookupStatus(str, enum.Enum):
    MAPPED = "MAPPED"
    PASSED_THROUGH = "PASSED_THROUGH"
    DEFAULTED = "DEFAULTED"
    LOOKUP_FAILED = "LOOKUP_FAILED"
    NULL_INPUT = "NULL_INPUT"


class LookupResult(_Model):
    source_value: Optional[str]
    target_value: Optional[str]
    status: LookupStatus


class LookupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    field_rule_id: int
    source_value: str
    target_value: str


# ------------------------------------------------------------------ transformation
class LineageMode(str, enum.Enum):
    ALL = "ALL"  # every cell of every audited rule
    EXCEPTIONS = "EXCEPTIONS"  # only cells whose value changed or that failed
    NONE = "NONE"


class RuleSummary(_Model):
    rule_id: int
    rule_type: str
    source_column: Optional[str]
    target: str  # TABLE.COLUMN
    staging_column: str
    failed_cells: int = 0


class TransformationResult(_Model):
    mapping_set_id: int
    source_path: str
    output_path: str
    pk_strategy: Literal["CATALOG_PK", "PARAMETER", "ROW_NUMBER"]
    source_records: int
    ok_records: int
    failed_records: int
    rules: list[RuleSummary]
    lineage_rows: int = 0
    passthrough_columns: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ deduplication
class ExactKey(_Model):
    """A composite business key. Rows with identical, non-empty normalised values are duplicates."""

    name: str
    columns: list[str] = Field(min_length=1)
    # alnum: tax/VAT ids (strip punctuation, upper) | email: trim+lower | digits: phone numbers
    # text: collapse whitespace + upper
    normalizer: Literal["alnum", "email", "digits", "text"] = "text"
    # Two records that both carry a valid value of this key and DIFFER are different entities: the fuzzy match may not merge them, however
    # alike their names are (two companies with different tax numbers). Off by default: e-mail and phone legitimately differ within one entity.
    vetoes_fuzzy: bool = False


class DedupConfig(_Model):
    pk_column: str = "_SRC_PK"
    status_column: Optional[str] = "_TRANSFORM_STATUS"  # rows marked FAILED are not deduplicated
    exact_keys: list[ExactKey] = Field(default_factory=list)
    name_column: Optional[str] = None
    address_columns: list[str] = Field(default_factory=list)
    # A key value shared by more records than this is a placeholder or group mailbox, not one entity: it links nobody.
    max_key_group: int = Field(default=25, ge=2)
    fuzzy_threshold: float = Field(default=0.88, ge=0.0, le=1.0)  # at or above: merged automatically
    # Fellegi-Sunter's third outcome: a pair scoring from here up to fuzzy_threshold is a POSSIBLE duplicate. It is NOT merged (both records load) and it is
    # listed in possible_duplicates.parquet for a person to decide. Equal to fuzzy_threshold = no review band.
    review_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    # A record that failed transformation can still be recognised as a duplicate of a good one (and then needs no fixing), but never becomes the master
    # of a group that has a good member.
    include_failed_rows: bool = True
    # What people decided about pairs of records (by their primary key value) in the review of possible duplicates: a decided pair is no longer left to the score.
    # Survivorship: when records are merged, what happens to a field the surviving record lacks. `master_only` (default): nothing, the master is loaded as it is (and what the
    # duplicates knew is lost). `fill_blanks`: a blank field of the master is filled from the best duplicate that has it (the most complete first); the report counts
    # the cells filled and every filled row says which columns came from a duplicate (dedup_enriched_columns).
    survivorship: Literal["master_only", "fill_blanks"] = "master_only"
    survivorship_columns: Optional[list[str]] = None  # default: every staging value column except the name, the keys and the internal ones
    must_link: list[tuple[str, str]] = Field(default_factory=list)    # 'these are the same entity': merged whatever the score
    cannot_link: list[tuple[str, str]] = Field(default_factory=list)  # 'these are different entities': never merged, whatever the score or the key
    name_weight: float = Field(default=0.65, gt=0.0, lt=1.0)
    max_block_size: int = Field(default=2000, ge=2)
    # A name that this many records (or more) share is a COMMON name ("Maria Garcia"): on its own, or with nothing more than a city, it proves nothing about two records being
    # one person. Such a pair is not merged: it goes to the review list, where a person decides. A rare name is still strong evidence.
    common_name_count: int = Field(default=5, ge=2)
    # Columns counted for the completeness score; default: every non-internal staging column.
    completeness_columns: Optional[list[str]] = None

    @model_validator(mode="after")
    def _needs_something(self) -> DedupConfig:
        if not self.exact_keys and not self.name_column:
            raise ValueError("configure at least one exact key or a fuzzy name_column")
        return self


class ClusterSample(_Model):
    cluster_id: int
    master_pk: str
    member_pks: list[str]
    match_type: str


class DedupReport(_Model):
    input_rows: int
    eligible_rows: int
    excluded_failed_rows: int
    duplicate_clusters: int
    duplicate_children: int
    surviving_records: int  # eligible rows - duplicate children (+ excluded rows are not counted)
    exact_match_clusters: int
    fuzzy_match_clusters: int
    exact_edges_by_key: dict[str, int]
    ignored_shared_keys: dict[str, int] = Field(default_factory=dict)  # key name -> distinct values skipped as placeholders
    fuzzy_pairs_evaluated: int
    fuzzy_pairs_matched: int
    possible_duplicate_pairs: int = 0  # pairs in the review band: NOT merged, listed for a person (possible_duplicates_path)
    possible_duplicates_path: Optional[str] = None
    enriched_cells: int = 0  # fields of a surviving record filled from a duplicate (survivorship = fill_blanks)
    reviewed_merges: int = 0  # pairs a person decided to merge, found in this data
    reviewed_separations: int = 0  # pairs a person decided to keep apart, found in this data
    weak_match_clusters: int = 0  # merged groups held together by the name alone (a record had no address to confirm it)
    fuzzy_blocks_limited: int = 0  # oversized blocks compared by nearest neighbours only (see max_block_size)
    street_conflict_pairs_held_back: int = 0  # name + city + postal code + house number matched but the STREET names are plainly different: sent to review instead of merged
    common_name_pairs_held_back: int = 0  # name matches NOT merged because the name is a common one and nothing but the city backed it: sent to review instead
    chain_links_refused: int = 0  # links NOT made because they would have put two different tax numbers into one group (reached through a record that has no tax number)
    exact_links_vetoed: int = 0  # shared e-mail / phone links NOT made because the two records carry different tax numbers (two legal entities)
    fuzzy_pairs_vetoed: int = 0  # name/address matches NOT merged because a veto key (tax number) differs between the two records
    samples: list[ClusterSample]
    output_path: str
    warnings: list[str] = Field(default_factory=list)  # things a person should look at before trusting the result (very high merge rate, merged records with different tax numbers)


# ------------------------------------------------------------------ run orchestration
class RunReport(_Model):
    run_id: int
    mapping_set_id: int
    wave_name: str
    status: str
    source_records: int  # every row of the file, including the ones an exclusion rule left behind
    excluded_records: int = 0  # rows left out on purpose by the mapping set's exclusion rules (kept in excluded.parquet with the reason)
    transformed_records: int
    failed_records: int
    lineage_rows: int
    started_at: datetime
    completed_at: datetime
    staging_path: str
    deduplicated_path: Optional[str] = None
    transformation: TransformationResult
    deduplication: Optional[DedupReport] = None
    report_path: Optional[str] = None
    message: Optional[str] = None
