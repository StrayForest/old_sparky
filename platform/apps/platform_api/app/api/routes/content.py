from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from apps.platform_api.app.api.schemas import (
    HomeContentResponse,
    DeadlockGameAssetsResponse,
    PatchDetailResponse,
    SupportMessageRequest,
    SupportMessageResponse,
    SupportStatusResponse,
)
from apps.platform_api.app.services.home_content import (
    public_deadlock_game_assets,
    resolve_deadlock_hero_image,
    resolve_deadlock_rank_image,
)
from apps.platform_api.app.services.home_content_security import (
    get_deadlock_asset_catalog,
    refresh_home_content,
)
from apps.platform_api.app.services.patch_detail_security import get_patch_detail
from apps.platform_api.app.services.support_mail import (
    send_support_message,
    support_mail_configured,
)
from python_packages.platform_domain.deadlock.constants import POOL_LIST, RANKS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client


router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/home", response_model=HomeContentResponse)
async def home_content(response: Response) -> HomeContentResponse:
    payload = await refresh_home_content()
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=900"
    return HomeContentResponse.model_validate(payload)


@router.get("/game-assets", response_model=DeadlockGameAssetsResponse)
async def deadlock_game_assets(
    request: Request,
    response: Response,
) -> DeadlockGameAssetsResponse | Response:
    catalog = await get_deadlock_asset_catalog()
    payload = public_deadlock_game_assets(catalog)
    etag = _game_assets_etag(payload)
    cache_headers = {
        "Cache-Control": "public, max-age=900, stale-while-revalidate=86400",
        "ETag": etag,
        "Vary": "Accept-Encoding",
    }
    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=cache_headers)
    for name, value in cache_headers.items():
        response.headers[name] = value
    return DeadlockGameAssetsResponse.model_validate(payload)


def _game_assets_etag(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    if not value:
        return False
    return any(
        candidate.strip() == "*"
        or candidate.strip().removeprefix("W/") == etag
        for candidate in value.split(",")
    )


@router.get("/game-assets/heroes/{hero_name}.png", response_model=None)
async def deadlock_hero_image(hero_name: str) -> RedirectResponse:
    catalog = await get_deadlock_asset_catalog()
    image_url = resolve_deadlock_hero_image(catalog, hero_name)
    if image_url is None:
        canonical_name = "The Doorman" if hero_name.casefold() == "doorman" else hero_name
        image_url = (
            f"/assets/heroes/{canonical_name.replace(' ', '_')}.png"
            if canonical_name in POOL_LIST
            else "/assets/heroes/placeholder.svg"
        )
    return RedirectResponse(
        image_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "public, max-age=3600, stale-while-revalidate=86400"},
    )


@router.get("/game-assets/ranks/{rank_name}.webp", response_model=None)
async def deadlock_rank_image(rank_name: str) -> RedirectResponse:
    catalog = await get_deadlock_asset_catalog()
    image_url = resolve_deadlock_rank_image(catalog, rank_name)
    if image_url is None:
        fallback_name = rank_name if rank_name in RANKS else "Initiate"
        image_url = f"/assets/ranks/{fallback_name}.webp"
    return RedirectResponse(
        image_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "public, max-age=3600, stale-while-revalidate=86400"},
    )


@router.get("/patches/{patch_id}", response_model=PatchDetailResponse)
async def patch_detail(patch_id: str, response: Response) -> PatchDetailResponse:
    if not patch_id.isdigit() or len(patch_id) > 32:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Патч не найден.")
    content = await get_patch_detail(patch_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Патч не найден.")
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return PatchDetailResponse.model_validate(content)


@router.get("/support/status", response_model=SupportStatusResponse)
async def support_status() -> SupportStatusResponse:
    return SupportStatusResponse(configured=support_mail_configured(get_settings()))


@router.post(
    "/support/messages",
    response_model=SupportMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_support_message(
    payload: SupportMessageRequest,
    request: Request,
) -> SupportMessageResponse:
    settings = get_settings()
    if not support_mail_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Отправка обращений временно недоступна.",
        )
    if payload.website:
        return SupportMessageResponse(accepted=True)

    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_address = forwarded_for.split(",")[-1].strip()
    if not client_address:
        client_address = request.client.host if request.client else "unknown"
    fingerprint = hashlib.sha256(client_address.encode("utf-8")).hexdigest()
    rate_key = f"platform:support-rate:v1:{fingerprint}"
    cache = redis_client()
    try:
        attempts = int(await cache.incr(rate_key))
        if attempts == 1:
            await cache.expire(rate_key, 3600)
    finally:
        await cache.aclose()
    if attempts > settings.platform_support_rate_limit_per_hour:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много обращений. Попробуйте позже.",
        )

    try:
        await send_support_message(
            settings,
            name=payload.name,
            reply_email=str(payload.email),
            category=payload.category,
            message=payload.message,
        )
    except Exception as exc:
        logger.warning("Support email delivery failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось отправить обращение. Попробуйте позже.",
        ) from exc
    return SupportMessageResponse(accepted=True)
