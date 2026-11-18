"""Authenticated model settings and draft directory discovery."""

from __future__ import annotations

import asyncio
from urllib.parse import unquote

from fastapi import APIRouter, Query, Request
from pydantic import SecretStr

from agent_workbench.config.model_discovery import DiscoveryError, fetch_catalog
from agent_workbench.config.model_settings import (
    ModelPreferences,
    StoredProvider,
    configuration_lock,
    discovery_url,
    model_origin,
    read_model_key,
    read_preferences,
    save_preferences,
    save_provider,
    validate_api_key,
    validate_model_url,
)
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.models import (
    ModelConnection,
    ModelDiscoveryRead,
    ModelDiscoveryRequest,
    ModelProfileRead,
    ModelSettingsRead,
    ModelSettingsUpdate,
    ProviderDraft,
    ProviderID,
    ProviderRead,
    ProviderWrite,
)
from agent_workbench.domain.prices import current_price_catalog
from agent_workbench.models.provider import catalog_profile
from agent_workbench.persistence.db import get_database

router = APIRouter(prefix="/api/settings/models", tags=["settings"])


def _authorize(request: Request, *, write: bool = False) -> None:
    from agent_workbench.api.routes import get_auth

    auth = get_auth()
    if write:
        auth.check_origin(request)
    ticket = auth.require_session(request)
    if write:
        auth.require_csrf(request, ticket)


def _invalid(field: str, message: str) -> ApiError:
    return ApiError(ErrorCode.VALIDATION_FAILED, message, details={"field": field})


def _key(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_api_key(value.get_secret_value())
    except ValueError as exc:
        raise _invalid("api_key", str(exc)) from None


def _connection(draft: ModelConnection) -> tuple[str, str | None]:
    try:
        base = validate_model_url(draft.base_url, inference=True)
    except ValueError as exc:
        raise _invalid("base_url", str(exc)) from None
    try:
        models = (
            discovery_url(draft, base)
            if draft.models_url or draft.discovery_format != "openai"
            else None
        )
    except ValueError as exc:
        raise _invalid("models_url", str(exc)) from None
    return base, models


def _vault_key(provider: StoredProvider, settings: Settings) -> str | None:
    try:
        return read_model_key(provider.credential_slot, settings)
    except (ValueError, OSError):
        raise ApiError(ErrorCode.INTERNAL_ERROR, "无法读取本机模型密钥库") from None


def _preferences(settings: Settings) -> ModelPreferences:
    try:
        return read_preferences(settings)
    except (ValueError, OSError):
        raise ApiError(ErrorCode.INTERNAL_ERROR, "无法读取本机模型设置文件") from None


async def _read_settings() -> ModelSettingsRead:
    settings = get_settings()
    async with get_database().session() as db:
        price_catalog = await current_price_catalog(db)
    with configuration_lock:
        preferences = _preferences(settings)
        providers = []
        profiles = []
        for provider in (preferences.providers or {}).values():
            configured = bool(_vault_key(provider, settings))
            providers.append(
                ProviderRead(
                    **provider.model_dump(exclude={"credential_slot", "adapter"}),
                    secret_configured=configured,
                )
            )
            for entry in provider.models:
                if provider.enabled and entry.enabled:
                    profile = catalog_profile(provider, entry, provider.base_url, settings)
                    profile_data = profile.model_dump(exclude={"request_output_limit"})
                    profiles.append(
                        ModelProfileRead(
                            **profile_data,
                            secret_configured=configured,
                            request_output_limit=profile.effective_output_limit,
                            allowed_input_tokens=int(
                                profile.input_budget_tokens
                                * (1 - profile.budget_safety_margin_ratio)
                            ),
                        )
                    )
    return ModelSettingsRead(
        default_provider=preferences.default_provider,
        default_model=preferences.default_model,
        profiles=profiles,
        providers=providers,
        revision=preferences.revision,
        price_catalog_version=price_catalog.version,
        effective_scope="主智能体和子智能体共用模型设置。新阶段使用当前选择，已有阶段保留其模型绑定。",
    )


@router.get("", response_model=ModelSettingsRead)
async def get_model_settings(request: Request) -> ModelSettingsRead:
    _authorize(request)
    return await _read_settings()


def _check_revision(preferences: ModelPreferences, revision: int) -> None:
    if revision != preferences.revision:
        raise ApiError(ErrorCode.CONFLICT, "模型设置已在其他窗口中更新，请刷新页面后编辑")


def _choose_default(
    preferences: ModelPreferences, preferred: tuple[str, str] | None = None
) -> None:
    candidates = [
        (p.provider_id, m.model_id)
        for p in (preferences.providers or {}).values()
        if p.enabled and p.base_url
        for m in p.models
        if m.enabled
    ]
    current = preferences.default_provider, preferences.default_model
    if preferred is not None and preferred not in candidates:
        raise _invalid("default_model", "只能将已启用的模型设为默认模型")
    if preferred is not None:
        selected = preferred
    elif current in candidates:
        selected = current
    else:
        selected = candidates[0] if candidates else ("", "")
    preferences.default_provider, preferences.default_model = selected


def _reject_key_in_fields(draft: ModelConnection, key: str | None) -> None:
    if key is None:
        return
    for field, value in draft.model_dump(exclude={"api_key"}).items():
        if isinstance(value, str) and (value == key or (len(key) >= 8 and key in unquote(value))):
            raise _invalid(field, "请只在 API Key 字段填写密钥")
    if isinstance(draft, ProviderDraft) and any(
        value == key or (len(key) >= 8 and key in value)
        for model in draft.models
        for value in (model.model_id, model.display_name)
    ):
        raise _invalid("models", "请只在 API Key 字段填写密钥")


def _store(
    preferences: ModelPreferences,
    draft: ProviderDraft,
    previous: StoredProvider | None,
    key: str | None,
    delete_key: bool,
    default_model: str | None,
    settings: Settings,
) -> None:
    base, models = _connection(draft)
    if key is not None and delete_key:
        raise _invalid("api_key", "清除 Key 时不能同时填写新 Key")
    _reject_key_in_fields(draft, key or (_vault_key(previous, settings) if previous else None))
    draft = draft.model_copy(
        update={"base_url": base, "models_url": models, "display_name": draft.display_name.strip()}
    )
    catalog = preferences.providers
    assert catalog is not None
    new_catalog = {**catalog, draft.provider_id: draft}
    updated = preferences.model_copy(
        update={"providers": new_catalog, "revision": preferences.revision + 1}
    )
    _choose_default(updated, (draft.provider_id, default_model) if default_model else None)
    try:
        save_provider(
            updated, draft, previous=previous, key=key, delete_key=delete_key, settings=settings
        )
    except ValueError as exc:
        raise _invalid("api_key", str(exc)) from None
    except OSError:
        raise ApiError(ErrorCode.INTERNAL_ERROR, "无法保存本机模型配置或密钥") from None


@router.post("/providers", response_model=ModelSettingsRead)
async def create_provider(request: Request, payload: ProviderWrite) -> ModelSettingsRead:
    return await _write_provider(request, payload)


@router.put("/providers/{provider_id}", response_model=ModelSettingsRead)
async def update_provider(
    request: Request, payload: ProviderWrite, provider_id: ProviderID
) -> ModelSettingsRead:
    return await _write_provider(request, payload, provider_id)


async def _write_provider(
    request: Request,
    payload: ProviderWrite,
    provider_id: str | None = None,
) -> ModelSettingsRead:
    _authorize(request, write=True)
    if len({model.model_id for model in payload.models}) != len(payload.models):
        raise _invalid("models", "同一提供商内的 Model ID 必须唯一")
    if provider_id is not None and provider_id != payload.provider_id:
        raise _invalid("provider_id", "保存后的 Provider ID 用于历史引用，不能修改")
    settings = get_settings()
    with configuration_lock:
        preferences = _preferences(settings)
        _check_revision(preferences, payload.revision)
        catalog = preferences.providers or {}
        previous = catalog.get(payload.provider_id)
        reserved = {pid.casefold() for pid in catalog.keys() | preferences.retired_provider_ids}
        if provider_id is None and payload.provider_id.casefold() in reserved:
            raise _invalid("provider_id", "此 Provider ID 已使用，请填写其他标识")
        if provider_id is not None and previous is None:
            raise ApiError(ErrorCode.NOT_FOUND, "提供商不存在")
        draft = ProviderDraft.model_validate(payload.model_dump(exclude={"api_key"}))
        _store(
            preferences,
            draft,
            previous,
            _key(payload.api_key),
            payload.delete_api_key,
            payload.default_model,
            settings,
        )
    return await _read_settings()


@router.delete("/providers/{provider_id}", response_model=ModelSettingsRead)
async def delete_provider(
    request: Request, provider_id: ProviderID, revision: int = Query()
) -> ModelSettingsRead:
    _authorize(request, write=True)
    settings = get_settings()
    with configuration_lock:
        preferences = _preferences(settings)
        _check_revision(preferences, revision)
        if preferences.providers is None or provider_id not in preferences.providers:
            raise ApiError(ErrorCode.NOT_FOUND, "提供商不存在")
        del preferences.providers[provider_id]
        preferences.retired_provider_ids.add(provider_id)
        preferences.revision += 1
        _choose_default(preferences)
        try:
            save_preferences(preferences, settings)
        except OSError:
            raise ApiError(ErrorCode.INTERNAL_ERROR, "无法保存本机模型配置") from None
    return await _read_settings()


@router.put("", response_model=ModelSettingsRead)
async def update_model_settings(
    request: Request, payload: ModelSettingsUpdate
) -> ModelSettingsRead:
    """Compatibility endpoint for callers that only change an existing model's connection."""
    _authorize(request, write=True)
    settings = get_settings()
    with configuration_lock:
        preferences = _preferences(settings)
        previous = (preferences.providers or {}).get(payload.provider_id)
        if previous is None or not any(m.model_id == payload.model_name for m in previous.models):
            raise _invalid("model_name", "请选择已配置的模型")
        draft = ProviderDraft.model_validate(previous.model_dump() | {"base_url": payload.base_url})
        _store(
            preferences,
            draft,
            previous,
            _key(payload.api_key),
            payload.delete_api_key,
            payload.model_name if payload.set_default else None,
            settings,
        )
    return await _read_settings()


async def _disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


@router.post("/discover", response_model=ModelDiscoveryRead)
async def discover_models(request: Request, payload: ModelDiscoveryRequest) -> ModelDiscoveryRead:
    _authorize(request, write=True)
    base, override = _connection(payload)
    target = override or base + "/models"
    key = _key(payload.api_key)
    if key is None and payload.saved_provider_id:
        settings = get_settings()
        with configuration_lock:
            provider = (_preferences(settings).providers or {}).get(payload.saved_provider_id)
            if (
                provider
                and provider.base_url
                and (model_origin(provider.base_url) == model_origin(base) == model_origin(target))
            ):
                saved_key = _vault_key(provider, settings)
                key = _key(SecretStr(saved_key)) if saved_key else None
            elif provider:
                raise _invalid("api_key", "推理或目录地址的主机已变更，请为本次查询输入 Key")
    if key is None:
        raise _invalid("api_key", "请填写 API Key 后查询模型目录")
    _reject_key_in_fields(payload, key)
    directory = asyncio.create_task(fetch_catalog(target, key, payload.discovery_format))
    disconnect = asyncio.create_task(_disconnect(request))
    try:
        done, _ = await asyncio.wait((directory, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if directory not in done:
            raise ApiError(ErrorCode.CANCELLED, "模型目录查询已取消")
        return directory.result()
    except DiscoveryError as exc:
        code = ErrorCode.TIMEOUT if exc.kind == "timeout" else ErrorCode.PROVIDER_ERROR
        raise ApiError(
            code,
            str(exc),
            retryable=exc.kind in {"timeout", "connection"} or exc.status == 429,
            details={"kind": exc.kind, "upstream_status": exc.status},
        ) from None
    finally:
        directory.cancel()
        disconnect.cancel()
        await asyncio.gather(directory, disconnect, return_exceptions=True)
