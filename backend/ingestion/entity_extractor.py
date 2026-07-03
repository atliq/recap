"""
RECAP v2 - Entity Extractor

Extracts named entities from text using spaCy NER.
Uses spaCy's built-in NER as the primary method. GLiNER can be
added later for zero-shot entity types.

Entities: PERSON, ORG, GPE (location), PRODUCT, WORK_OF_ART,
          EVENT, LAW, LANGUAGE, FAC (facility)
"""

from __future__ import annotations

import logging
from typing import List, Set

from backend.models import EntityData

logger = logging.getLogger(__name__)

# Map spaCy entity labels to our taxonomy
ENTITY_TYPE_MAP = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "location",
    "LOC": "location",
    "FAC": "facility",
    "PRODUCT": "product",
    "WORK_OF_ART": "work",
    "EVENT": "event",
    "LAW": "law",
    "LANGUAGE": "language",
    "NORP": "group",  # Nationalities, religious/political groups
    "MONEY": "skip",
    "QUANTITY": "skip",
    "ORDINAL": "skip",
    "CARDINAL": "skip",
    "PERCENT": "skip",
    "DATE": "skip",
    "TIME": "skip",
}

# Minimum entity name length to avoid noise
MIN_ENTITY_LENGTH = 2
# Maximum entity name length to avoid garbage
MAX_ENTITY_LENGTH = 100


class EntityExtractor:
    """Extracts named entities from text using spaCy NER."""

    def __init__(self, nlp=None):
        """
        Args:
            nlp: spaCy language model. If None, entity extraction is disabled.
        """
        self.nlp = nlp
        if nlp is None:
            logger.warning("No spaCy model provided - entity extraction disabled")

    def extract(
        self,
        text: str,
        source_url: str = "",
        source_chunk_id: str = "",
    ) -> List[EntityData]:
        """
        Extract entities from text.

        Args:
            text: Text to extract entities from.
            source_url: URL of the source page.
            source_chunk_id: Chunk ID for provenance.

        Returns:
            List of deduplicated EntityData objects.
        """
        if not self.nlp or not text or not text.strip():
            return []

        try:
            return self._extract_spacy(text, source_url, source_chunk_id)
        except Exception as e:
            logger.error("Entity extraction failed: %s", e)
            return []

    def _extract_spacy(
        self,
        text: str,
        source_url: str,
        source_chunk_id: str,
    ) -> List[EntityData]:
        """Extract entities using spaCy NER."""
        # Limit text length for processing
        max_length = 100_000
        if len(text) > max_length:
            text = text[:max_length]

        doc = self.nlp(text)

        seen: Set[str] = set()
        entities = []

        for ent in doc.ents:
            # Map to our taxonomy
            entity_type = ENTITY_TYPE_MAP.get(ent.label_, None)
            if entity_type is None or entity_type == "skip":
                continue

            # Clean entity name
            name = ent.text.strip()
            name = name.replace("\n", " ").replace("\t", " ")

            # Validate
            if len(name) < MIN_ENTITY_LENGTH or len(name) > MAX_ENTITY_LENGTH:
                continue

            # Skip entities that are just numbers or very common words
            if name.isdigit():
                continue

            # Deduplicate by normalized name
            normalized = name.lower()
            if normalized in seen:
                continue
            seen.add(normalized)

            entities.append(EntityData(
                name=name,
                entity_type=entity_type,
                source_url=source_url,
                source_chunk_id=source_chunk_id,
                confidence=1.0,
            ))

        logger.debug(
            "Extracted %d entities from %s",
            len(entities),
            source_url or "text",
        )
        return entities
