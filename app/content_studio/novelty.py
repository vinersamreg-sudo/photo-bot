"""Persistent duplicate and novelty checks for rights-cleared demo posts."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image

from .content_generator import DISCLOSURE
from .repository import ContentStudioRepository
from .storage import ContentStorage


HISTORY_LIMIT = 30
MAX_CANDIDATE_ATTEMPTS = 5
TEXT_SIMILARITY_THRESHOLD = 0.82
PHASH_MAX_DISTANCE = 6

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"(?<!\w)#[\w\-]+", re.UNICODE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_TECHNICAL_ID_RE = re.compile(
    r"\b(?:src|post|external|utm|event|attempt)[_:\-=][\w.\-:]+\b",
    re.IGNORECASE,
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[t\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+\-]\d{2}:?\d{2})?)?\b",
    re.IGNORECASE,
)


class DuplicateContentError(RuntimeError):
    """Raised before transport use when a post fails the novelty contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Content Studio novelty gate blocked publication: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class NoveltyFingerprint:
    post_id: str
    platform: str
    asset_id: str
    asset_checksum: str
    transformation_type: str
    category: str
    before_phash: str
    after_phash: str
    card_phash: str
    normalized_text_hash: str
    normalized_text: str
    scheduled_time: str | None

    def record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NoveltyDecision:
    allowed: bool
    score: float
    reason: str | None = None
    matched_post_id: str | None = None


def normalize_caption(
    title: str,
    body: str,
    *,
    disclosure: str = DISCLOSURE,
) -> str:
    """Return semantic demo copy without attribution or boilerplate noise."""

    value = unicodedata.normalize("NFKC", f"{title}\n{body}").casefold()
    for boilerplate in (
        disclosure,
        DISCLOSURE,
        "попробовать ravuna",
        "получите 2 бесплатные обработки",
    ):
        if boilerplate:
            value = value.replace(
                unicodedata.normalize("NFKC", boilerplate).casefold(), " "
            )
    value = _URL_RE.sub(" ", value)
    value = _HASHTAG_RE.sub(" ", value)
    value = _UUID_RE.sub(" ", value)
    value = _TECHNICAL_ID_RE.sub(" ", value)
    value = _TIMESTAMP_RE.sub(" ", value)
    value = "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in value
    )
    return " ".join(value.split())


def normalized_text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    character_ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    token_ratio = (
        2 * len(left_tokens & right_tokens) / (len(left_tokens) + len(right_tokens))
        if left_tokens and right_tokens
        else 0.0
    )
    return max(character_ratio, token_ratio)


def perceptual_hash(path: Path) -> str:
    """Compute a 64-bit difference hash, stable across resize/re-encode."""

    with Image.open(path) as image:
        grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(grayscale.tobytes())
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f"{bits:016x}"


def phash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def evaluate_fingerprint(
    candidate: NoveltyFingerprint,
    history: Sequence[dict[str, Any]],
    *,
    alternative_categories: Iterable[str] = (),
) -> NoveltyDecision:
    """Compare one candidate with the most recent same-platform history."""

    recent = [row for row in history if row.get("platform") == candidate.platform][
        :HISTORY_LIMIT
    ]
    max_text_similarity = 0.0
    for row in recent:
        matched = str(row.get("post_id") or "") or None
        if row.get("asset_checksum") == candidate.asset_checksum:
            return NoveltyDecision(False, 0.0, "asset_checksum_reused", matched)
        card_hash = str(row.get("card_phash") or "")
        if card_hash and phash_distance(candidate.card_phash, card_hash) <= PHASH_MAX_DISTANCE:
            return NoveltyDecision(False, 0.0, "visual_near_duplicate", matched)
        if row.get("normalized_text_hash") == candidate.normalized_text_hash:
            return NoveltyDecision(False, 0.0, "text_exact_duplicate", matched)
        similarity = text_similarity(
            candidate.normalized_text, str(row.get("normalized_text") or "")
        )
        max_text_similarity = max(max_text_similarity, similarity)
        if similarity >= TEXT_SIMILARITY_THRESHOLD:
            return NoveltyDecision(
                False,
                round(1.0 - similarity, 4),
                "text_near_duplicate",
                matched,
            )
        if (
            candidate.scheduled_time
            and row.get("scheduled_time") == candidate.scheduled_time
        ):
            return NoveltyDecision(False, 0.0, "publication_slot_occupied", matched)

    last_three = recent[:3]
    alternatives = {value for value in alternative_categories if value}
    if len(last_three) == 3 and alternatives - {candidate.category}:
        same_transformation = all(
            row.get("transformation_type") == candidate.transformation_type
            for row in last_three
        )
        same_category = all(row.get("category") == candidate.category for row in last_three)
        if same_transformation or same_category:
            return NoveltyDecision(False, 0.0, "recent_category_saturation")
    return NoveltyDecision(True, round(1.0 - max_text_similarity, 4))


class DuplicateNoveltyGate:
    def __init__(
        self, repository: ContentStudioRepository, storage: ContentStorage
    ) -> None:
        self.repository = repository
        self.storage = storage

    def fingerprint(self, post_id: str) -> NoveltyFingerprint:
        row = self.repository.novelty_candidate(post_id)
        before = self.storage.resolve(str(row["before_path"]))
        after = self.storage.resolve(str(row["after_path"]))
        card = self.storage.resolve(str(row["watermark_preview_path"]))
        if card.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            card = after
        normalized = normalize_caption(
            str(row["title"]),
            str(row["body"]),
            disclosure=str(row.get("disclosure") or ""),
        )
        if not normalized:
            raise ValueError("Content Studio normalized publication text is empty")
        return NoveltyFingerprint(
            post_id=post_id,
            platform=str(row["platform"]),
            asset_id=str(row["asset_id"]),
            asset_checksum=str(row["asset_checksum"]),
            transformation_type=str(row["transformation_type"]),
            category=str(row["asset_category"]),
            before_phash=perceptual_hash(before),
            after_phash=perceptual_hash(after),
            card_phash=perceptual_hash(card),
            normalized_text_hash=normalized_text_hash(normalized),
            normalized_text=normalized,
            scheduled_time=row.get("scheduled_time") or row.get("published_time"),
        )

    def evaluate(
        self,
        post_id: str,
        *,
        alternative_categories: Iterable[str] = (),
    ) -> tuple[NoveltyFingerprint, NoveltyDecision]:
        fingerprint = self.fingerprint(post_id)
        if fingerprint.scheduled_time:
            owner = self.repository.publication_slot_owner(
                fingerprint.platform, fingerprint.scheduled_time
            )
            if owner and owner != post_id:
                return fingerprint, NoveltyDecision(
                    False,
                    0.0,
                    "publication_slot_conflict",
                    owner,
                )
        decision = evaluate_fingerprint(
            fingerprint,
            self.repository.publication_history(
                platform=fingerprint.platform, limit=HISTORY_LIMIT
            ),
            alternative_categories=alternative_categories,
        )
        return fingerprint, decision

    def ensure_published_history(self) -> int:
        """Backfill legacy real publications before evaluating a new send."""

        created = 0
        for row in self.repository.published_candidates_missing_history():
            fingerprint = self.fingerprint(str(row["id"]))
            decision = evaluate_fingerprint(
                fingerprint,
                self.repository.publication_history(
                    platform=fingerprint.platform, limit=HISTORY_LIMIT
                ),
            )
            self.repository.record_publication_history(
                fingerprint.record(),
                external_id=str(row.get("published_external_id") or ""),
                published_at=str(row.get("published_time") or row.get("updated_at")),
                novelty_score=decision.score,
                duplicate_reason=decision.reason,
            )
            created += 1
        return created

