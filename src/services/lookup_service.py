"""Value-lookup registry on `map_value_lookups` plus a small-batch resolver.

Volume translation inside the transformation engine is done by DuckDB joins; it uses
`get_normalized_lookup` from here so both paths share one matching rule:
**trimmed, case-insensitive** comparison of the source value.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import RuleType
from src.db.models import MapFieldRule, MapValueLookup
from src.schemas.execution import LookupFallback, LookupRead, LookupResult, LookupStatus
from src.services.metadata_service import ConflictError, NotFoundError

COUNTRY_SEED: dict[str, str] = {
    "USA": "US", "U.S.A": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "US": "US",
    "GERMANY": "DE", "DEUTSCHLAND": "DE", "DE": "DE",
    "FRANCE": "FR", "FR": "FR",
    "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB", "GB": "GB",
    "INDIA": "IN", "IN": "IN",
    "CANADA": "CA", "CA": "CA",
}
PAYMENT_TERMS_SEED: dict[str, str] = {
    "NET30": "NT30", "NET 30": "NT30", "NET60": "NT60", "NET 60": "NT60", "NET90": "NT90",
    "COD": "CASH", "IMMEDIATE": "CASH", "2/10 NET 30": "NT3D",
}
STANDARD_SEEDS = {"COUNTRY": COUNTRY_SEED, "PAYMENT_TERMS": PAYMENT_TERMS_SEED}


def normalize_lookup_key(value: str) -> str:
    return value.strip().upper()


class LookupService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ registration
    def register_lookup(self, field_rule_id: int, source_value: str, target_value: str) -> LookupRead:
        """Create or update one translation."""
        self.register_lookups(field_rule_id, {source_value: target_value})
        return next(r for r in self.list_lookups(field_rule_id) if r.source_value == source_value.strip())

    def register_lookups(self, field_rule_id: int, mapping: Mapping[str, str]) -> int:
        """Upsert many translations in one statement; returns the number of entries written.

        Entries are rejected if two source values collide after normalisation with different
        targets (e.g. 'usa' -> 'US' and 'USA' -> 'UK'), since matching ignores case.
        """
        cleaned: dict[str, str] = {}
        seen: dict[str, tuple[str, str]] = {}
        for raw_source, raw_target in mapping.items():
            source, target = raw_source.strip(), raw_target.strip()
            if not source:
                raise ValueError("source_value must not be empty")
            if not target:
                raise ValueError(f"target_value for '{source}' must not be empty")
            key = normalize_lookup_key(source)
            if key in seen and seen[key][1] != target:
                raise ConflictError(
                    f"'{source}' and '{seen[key][0]}' are the same lookup key but map to different targets"
                )
            seen[key] = (source, target)
            cleaned[source] = target
        if not cleaned:
            return 0

        with self._session_factory.begin() as session:
            self._require_value_mapping_rule(session, field_rule_id)
            existing = {
                normalize_lookup_key(s): (s, t)
                for s, t in session.execute(
                    select(MapValueLookup.source_value, MapValueLookup.target_value).where(
                        MapValueLookup.field_rule_id == field_rule_id
                    )
                )
            }
            for source, target in cleaned.items():
                prior = existing.get(normalize_lookup_key(source))
                if prior and prior[0] != source and prior[1] != target:
                    raise ConflictError(f"'{source}' collides case-insensitively with stored '{prior[0]}'")
            stmt = pg_insert(MapValueLookup).values(
                [{"field_rule_id": field_rule_id, "source_value": s, "target_value": t}
                 for s, t in cleaned.items()]
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_map_value_lookups_rule_source_value",
                set_={"target_value": stmt.excluded.target_value},
            )
            session.execute(stmt)
        return len(cleaned)

    def seed_standard_lookups(self, field_rule_id: int, kind: str) -> int:
        """Load a built-in translation set ('COUNTRY' or 'PAYMENT_TERMS') for a rule."""
        try:
            seed = STANDARD_SEEDS[kind.upper()]
        except KeyError:
            raise ValueError(f"unknown seed '{kind}', expected one of {sorted(STANDARD_SEEDS)}") from None
        return self.register_lookups(field_rule_id, seed)

    def clear_lookups(self, field_rule_id: int) -> int:
        with self._session_factory.begin() as session:
            self._require_value_mapping_rule(session, field_rule_id)
            return session.execute(
                delete(MapValueLookup).where(MapValueLookup.field_rule_id == field_rule_id)
            ).rowcount

    # ------------------------------------------------------------------ retrieval
    def list_lookups(self, field_rule_id: int) -> list[LookupRead]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(MapValueLookup)
                .where(MapValueLookup.field_rule_id == field_rule_id)
                .order_by(MapValueLookup.source_value)
            ).all()
            return [LookupRead.model_validate(r) for r in rows]

    def get_lookups(self, field_rule_id: int) -> dict[str, str]:
        return {r.source_value: r.target_value for r in self.list_lookups(field_rule_id)}

    def get_normalized_lookup(self, field_rule_id: int) -> dict[str, str]:
        """Matching table: normalised source value -> target code. Raises on ambiguous entries."""
        result: dict[str, str] = {}
        for source, target in self.get_lookups(field_rule_id).items():
            key = normalize_lookup_key(source)
            if key in result and result[key] != target:
                raise ConflictError(
                    f"rule {field_rule_id}: lookup key '{key}' maps to both '{result[key]}' and '{target}'"
                )
            result[key] = target
        return result

    # ------------------------------------------------------------------ resolution
    def apply_value_lookups(
        self,
        field_rule_id: int,
        values: Sequence[Optional[str]],
        fallback: LookupFallback = LookupFallback.FLAG_FAILED,
        default_value: Optional[str] = None,
    ) -> list[LookupResult]:
        """Resolve source values to target codes, one result per input value (order preserved)."""
        if fallback == LookupFallback.USE_DEFAULT and not default_value:
            raise ValueError("USE_DEFAULT fallback needs default_value")
        table = self.get_normalized_lookup(field_rule_id)
        results: list[LookupResult] = []
        for value in values:
            if value is None or not value.strip():
                results.append(LookupResult(source_value=value, target_value=None, status=LookupStatus.NULL_INPUT))
                continue
            hit = table.get(normalize_lookup_key(value))
            if hit is not None:
                results.append(LookupResult(source_value=value, target_value=hit, status=LookupStatus.MAPPED))
            elif fallback == LookupFallback.PASS_THROUGH:
                results.append(LookupResult(source_value=value, target_value=value.strip(),
                                            status=LookupStatus.PASSED_THROUGH))
            elif fallback == LookupFallback.USE_DEFAULT:
                results.append(LookupResult(source_value=value, target_value=default_value,
                                            status=LookupStatus.DEFAULTED))
            else:
                results.append(LookupResult(source_value=value, target_value=None,
                                            status=LookupStatus.LOOKUP_FAILED))
        return results

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _require_value_mapping_rule(session: Session, field_rule_id: int) -> None:
        rule = session.get(MapFieldRule, field_rule_id)
        if rule is None:
            raise NotFoundError(f"field rule {field_rule_id} does not exist")
        if rule.rule_type != RuleType.VALUE_MAPPING:
            raise ValueError(f"field rule {field_rule_id} is {rule.rule_type.value}, not VALUE_MAPPING")
