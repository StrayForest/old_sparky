from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import (
    MediaAsset,
    MediaVariant,
    PlayerProfile,
    Tournament,
    new_uuid,
)


MEDIA_PURPOSES: Final = frozenset(
    {"profile_avatar", "profile_banner", "tournament_banner"}
)
TERMINAL_MEDIA_STATUSES: Final = frozenset({"ready", "failed", "replaced", "deleted"})


@dataclass(frozen=True)
class VariantRecord:
    variant_name: str
    object_key: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class AssetDescriptor:
    asset_id: str
    purpose: str
    status: str
    error_code: str | None
    variants: tuple[VariantRecord, ...]


class MediaRepository:
    def __init__(self, db_session: AsyncSession) -> None:
        self.db_session = db_session

    def add_pending_asset(
        self,
        *,
        asset_id: str,
        purpose: str,
        owner_user_id: str | None,
        tournament_id: str | None,
        source_mime: str,
        source_bytes: int,
        source_sha256: str,
    ) -> MediaAsset:
        self._validate_ownership(
            purpose=purpose,
            owner_user_id=owner_user_id,
            tournament_id=tournament_id,
        )
        asset = MediaAsset(
            id=asset_id,
            owner_user_id=owner_user_id,
            tournament_id=tournament_id,
            purpose=purpose,
            status="pending",
            source_mime=source_mime,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            version_id=new_uuid(),
            attempt_count=0,
        )
        self.db_session.add(asset)
        return asset

    async def get_asset(
        self, asset_id: str, *, for_update: bool = False
    ) -> MediaAsset | None:
        statement = select(MediaAsset).where(MediaAsset.id == asset_id)
        if for_update:
            statement = statement.with_for_update()
        return await self.db_session.scalar(statement)

    async def claim_for_processing(
        self,
        asset_id: str,
        *,
        now: datetime,
        stale_before: datetime,
        max_attempts: int,
    ) -> MediaAsset | None:
        claimable = or_(
            and_(
                MediaAsset.status == "pending",
                or_(
                    MediaAsset.next_retry_at.is_(None), MediaAsset.next_retry_at <= now
                ),
            ),
            and_(
                MediaAsset.status == "processing",
                MediaAsset.processing_started_at <= stale_before,
            ),
        )
        statement = (
            update(MediaAsset)
            .where(
                MediaAsset.id == asset_id,
                MediaAsset.attempt_count < max_attempts,
                claimable,
            )
            .values(
                status="processing",
                error_code=None,
                attempt_count=MediaAsset.attempt_count + 1,
                processing_started_at=now,
                next_retry_at=None,
                updated_at=now,
            )
            .returning(MediaAsset)
        )
        result = await self.db_session.execute(statement)
        return result.scalar_one_or_none()

    async def finalize_ready(
        self,
        asset_id: str,
        variants: tuple[VariantRecord, ...],
        *,
        now: datetime,
        cleanup_grace: timedelta,
    ) -> str | None:
        asset = await self.get_asset(asset_id)
        if asset is None:
            raise RuntimeError("Media asset is missing")
        target, field_name, legacy_field = await self._lock_target(asset)
        asset = await self.get_asset(asset_id, for_update=True)
        if asset is None or asset.status != "processing":
            raise RuntimeError("Media asset is not in processing state")
        if not variants:
            raise RuntimeError("A ready media asset must have variants")
        existing_variant_id = await self.db_session.scalar(
            select(MediaVariant.id).where(MediaVariant.asset_id == asset_id).limit(1)
        )
        if existing_variant_id is not None:
            raise RuntimeError("Media variants already exist for this asset")

        old_asset_id = getattr(target, field_name)
        setattr(target, field_name, asset.id)
        setattr(target, legacy_field, None)

        for variant in variants:
            self.db_session.add(
                MediaVariant(
                    id=new_uuid(),
                    asset_id=asset.id,
                    variant_name=variant.variant_name,
                    object_key=variant.object_key,
                    mime_type=variant.mime_type,
                    width=variant.width,
                    height=variant.height,
                    byte_size=variant.byte_size,
                    sha256=variant.sha256,
                )
            )
        asset.status = "ready"
        asset.error_code = None
        asset.processing_started_at = None
        asset.next_retry_at = None
        asset.ready_at = now
        asset.updated_at = now

        if old_asset_id and old_asset_id != asset.id:
            old_asset = await self.get_asset(old_asset_id, for_update=True)
            if old_asset is not None and old_asset.status in {"ready", "replaced"}:
                old_asset.status = "replaced"
                old_asset.cleanup_after = now + cleanup_grace
                old_asset.updated_at = now
        await self.db_session.flush()
        return old_asset_id

    async def supersede_inflight_for_new_asset(
        self,
        *,
        purpose: str,
        owner_user_id: str | None,
        tournament_id: str | None,
        now: datetime,
        cleanup_grace: timedelta,
    ) -> tuple[str, ...]:
        self._validate_ownership(
            purpose=purpose,
            owner_user_id=owner_user_id,
            tournament_id=tournament_id,
        )
        if purpose in {"profile_avatar", "profile_banner"}:
            target = await self.db_session.scalar(
                select(PlayerProfile)
                .where(PlayerProfile.user_id == owner_user_id)
                .with_for_update()
            )
            owner_filter = MediaAsset.owner_user_id == owner_user_id
        else:
            target = await self.db_session.scalar(
                select(Tournament)
                .where(Tournament.id == tournament_id)
                .with_for_update()
            )
            owner_filter = MediaAsset.tournament_id == tournament_id
        if target is None:
            raise RuntimeError("Media owner does not exist")
        rows = await self.db_session.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.purpose == purpose,
                owner_filter,
                MediaAsset.status.in_(("pending", "processing")),
            )
            .with_for_update()
        )
        superseded: list[str] = []
        for asset in rows:
            asset.status = "replaced"
            asset.processing_started_at = None
            asset.next_retry_at = None
            asset.cleanup_after = now + cleanup_grace
            asset.updated_at = now
            superseded.append(asset.id)
        await self.db_session.flush()
        return tuple(superseded)

    async def mark_retry_or_failed(
        self,
        asset_id: str,
        *,
        error_code: str,
        retriable: bool,
        now: datetime,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> str:
        asset = await self.get_asset(asset_id, for_update=True)
        if asset is None:
            return "missing"
        if asset.status != "processing":
            return asset.status
        if not retriable or asset.attempt_count >= max_attempts:
            asset.status = "failed"
            asset.error_code = error_code
            asset.processing_started_at = None
            asset.next_retry_at = None
        else:
            retry_delay = min(
                retry_max_seconds,
                retry_base_seconds * (2 ** max(0, asset.attempt_count - 1)),
            )
            asset.status = "pending"
            asset.error_code = error_code
            asset.processing_started_at = None
            asset.next_retry_at = now + timedelta(seconds=retry_delay)
        asset.updated_at = now
        await self.db_session.flush()
        return asset.status

    async def fail_exhausted_processing(
        self,
        *,
        stale_before: datetime,
        max_attempts: int,
        now: datetime,
    ) -> int:
        result = await self.db_session.execute(
            update(MediaAsset)
            .where(
                MediaAsset.status == "processing",
                MediaAsset.processing_started_at <= stale_before,
                MediaAsset.attempt_count >= max_attempts,
            )
            .values(
                status="failed",
                error_code="media_processing_attempts_exhausted",
                processing_started_at=None,
                next_retry_at=None,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    async def reconcilable_asset_ids(
        self,
        *,
        now: datetime,
        stale_before: datetime,
        max_attempts: int,
        limit: int,
    ) -> tuple[str, ...]:
        rows = await self.db_session.scalars(
            select(MediaAsset.id)
            .where(
                MediaAsset.attempt_count < max_attempts,
                or_(
                    and_(
                        MediaAsset.status == "pending",
                        or_(
                            MediaAsset.next_retry_at.is_(None),
                            MediaAsset.next_retry_at <= now,
                        ),
                    ),
                    and_(
                        MediaAsset.status == "processing",
                        MediaAsset.processing_started_at <= stale_before,
                    ),
                ),
            )
            .order_by(MediaAsset.updated_at, MediaAsset.id)
            .limit(limit)
        )
        return tuple(rows)

    async def cleanup_candidate_ids(
        self, *, now: datetime, limit: int
    ) -> tuple[str, ...]:
        rows = await self.db_session.scalars(
            select(MediaAsset.id)
            .where(
                MediaAsset.status == "replaced",
                MediaAsset.cleanup_after.is_not(None),
                MediaAsset.cleanup_after <= now,
            )
            .order_by(MediaAsset.cleanup_after, MediaAsset.id)
            .limit(limit)
        )
        return tuple(rows)

    async def variant_object_keys(self, asset_id: str) -> tuple[str, ...]:
        rows = await self.db_session.scalars(
            select(MediaVariant.object_key)
            .where(MediaVariant.asset_id == asset_id)
            .order_by(MediaVariant.variant_name)
        )
        return tuple(rows)

    async def mark_deleted(self, asset_id: str, *, now: datetime) -> bool:
        result = await self.db_session.execute(
            update(MediaAsset)
            .where(MediaAsset.id == asset_id, MediaAsset.status == "replaced")
            .values(status="deleted", cleanup_after=None, updated_at=now)
        )
        return bool(result.rowcount)

    async def unlink_active_asset(
        self,
        *,
        purpose: str,
        owner_id: str,
        now: datetime,
        cleanup_grace: timedelta,
    ) -> str | None:
        if purpose in {"profile_avatar", "profile_banner"}:
            target = await self.db_session.scalar(
                select(PlayerProfile)
                .where(PlayerProfile.user_id == owner_id)
                .with_for_update()
            )
            if target is None:
                return None
            field_name = (
                "avatar_asset_id" if purpose == "profile_avatar" else "banner_asset_id"
            )
            legacy_field = "avatar_url" if purpose == "profile_avatar" else "banner_url"
            owner_filter = MediaAsset.owner_user_id == owner_id
        elif purpose == "tournament_banner":
            target = await self.db_session.scalar(
                select(Tournament).where(Tournament.id == owner_id).with_for_update()
            )
            if target is None:
                return None
            field_name = "banner_asset_id"
            legacy_field = "cover_url"
            owner_filter = MediaAsset.tournament_id == owner_id
        else:
            raise ValueError(f"Unsupported media purpose: {purpose}")

        asset_id = getattr(target, field_name)
        setattr(target, field_name, None)
        setattr(target, legacy_field, None)
        if asset_id:
            asset = await self.get_asset(asset_id, for_update=True)
            if asset is not None and asset.status in {"ready", "replaced"}:
                asset.status = "replaced"
                asset.cleanup_after = now + cleanup_grace
                asset.updated_at = now
        inflight_assets = tuple(
            await self.db_session.scalars(
                select(MediaAsset)
                .where(
                    MediaAsset.purpose == purpose,
                    owner_filter,
                    MediaAsset.status.in_(("pending", "processing")),
                )
                .order_by(MediaAsset.updated_at.desc(), MediaAsset.id.desc())
                .with_for_update()
            )
        )
        for inflight_asset in inflight_assets:
            inflight_asset.status = "replaced"
            inflight_asset.processing_started_at = None
            inflight_asset.next_retry_at = None
            inflight_asset.cleanup_after = now + cleanup_grace
            inflight_asset.updated_at = now
        await self.db_session.flush()
        return asset_id or (inflight_assets[0].id if inflight_assets else None)

    async def asset_statuses(self, asset_ids: tuple[str, ...]) -> dict[str, str]:
        if not asset_ids:
            return {}
        rows = await self.db_session.execute(
            select(MediaAsset.id, MediaAsset.status).where(MediaAsset.id.in_(asset_ids))
        )
        return {asset_id: status for asset_id, status in rows}

    async def descriptor(self, asset_id: str) -> AssetDescriptor | None:
        return (await self.descriptors((asset_id,))).get(asset_id)

    async def descriptors(
        self, asset_ids: tuple[str, ...]
    ) -> dict[str, AssetDescriptor]:
        unique_ids = tuple(dict.fromkeys(asset_ids))
        if not unique_ids:
            return {}
        assets = tuple(
            await self.db_session.scalars(
                select(MediaAsset).where(MediaAsset.id.in_(unique_ids))
            )
        )
        variants = tuple(
            await self.db_session.scalars(
                select(MediaVariant)
                .where(MediaVariant.asset_id.in_(unique_ids))
                .order_by(
                    MediaVariant.asset_id,
                    MediaVariant.width,
                    MediaVariant.variant_name,
                )
            )
        )
        variants_by_asset: dict[str, list[VariantRecord]] = {}
        for variant in variants:
            variants_by_asset.setdefault(variant.asset_id, []).append(
                VariantRecord(
                    variant_name=variant.variant_name,
                    object_key=variant.object_key,
                    mime_type=variant.mime_type,
                    width=variant.width,
                    height=variant.height,
                    byte_size=variant.byte_size,
                    sha256=variant.sha256,
                )
            )
        return {
            asset.id: AssetDescriptor(
                asset_id=asset.id,
                purpose=asset.purpose,
                status=asset.status,
                error_code=asset.error_code,
                variants=tuple(variants_by_asset.get(asset.id, ())),
            )
            for asset in assets
        }

    async def _lock_target(self, asset: MediaAsset) -> tuple[object, str, str]:
        if asset.purpose in {"profile_avatar", "profile_banner"}:
            target = await self.db_session.scalar(
                select(PlayerProfile)
                .where(PlayerProfile.user_id == asset.owner_user_id)
                .with_for_update()
            )
            if target is None:
                raise RuntimeError("Media profile owner no longer exists")
            if asset.purpose == "profile_avatar":
                return target, "avatar_asset_id", "avatar_url"
            return target, "banner_asset_id", "banner_url"
        if asset.purpose == "tournament_banner":
            target = await self.db_session.scalar(
                select(Tournament)
                .where(Tournament.id == asset.tournament_id)
                .with_for_update()
            )
            if target is None:
                raise RuntimeError("Media tournament owner no longer exists")
            return target, "banner_asset_id", "cover_url"
        raise RuntimeError("Media asset purpose is invalid")

    @staticmethod
    def _validate_ownership(
        *,
        purpose: str,
        owner_user_id: str | None,
        tournament_id: str | None,
    ) -> None:
        if purpose not in MEDIA_PURPOSES:
            raise ValueError(f"Unsupported media purpose: {purpose}")
        if purpose in {"profile_avatar", "profile_banner"}:
            valid = owner_user_id is not None and tournament_id is None
        else:
            valid = owner_user_id is None and tournament_id is not None
        if not valid:
            raise ValueError("Media ownership does not match its purpose")
