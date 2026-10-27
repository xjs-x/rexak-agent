"""价格配置版本的读取与更新；历史版本只读。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.config.catalog import ModelPrice, PriceCatalog
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.tables import ModelCallRow, PriceCatalogVersionRow, RunRow


class PriceUpdate(BaseModel):
    """一次模型单价修改；只接受每百万 token 单价。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: str = Field(min_length=1, max_length=64)
    provider_id: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_1m: Decimal = Field(ge=0, allow_inf_nan=False)
    output_per_1m: Decimal = Field(ge=0, allow_inf_nan=False)
    cache_read_per_1m: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_per_1m: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_tokens_included_in_input: bool = False
    reasoning_tokens_included_in_output: bool = False

    def to_model_price(self) -> ModelPrice:
        return ModelPrice(
            provider_id=self.provider_id,
            model_name=self.model_name,
            currency=self.currency,
            effective_at=datetime.now(UTC).isoformat(),
            input_per_unit=self.input_per_1m,
            output_per_unit=self.output_per_1m,
            cache_read_per_unit=self.cache_read_per_1m,
            cache_write_per_unit=self.cache_write_per_1m,
            cache_tokens_included_in_input=self.cache_tokens_included_in_input,
            reasoning_tokens_included_in_output=self.reasoning_tokens_included_in_output,
        )


def _catalog(row: PriceCatalogVersionRow) -> PriceCatalog:
    return PriceCatalog(
        version=row.version,
        prices=tuple(ModelPrice.model_validate(item) for item in row.prices),
    )


async def current_price_catalog(db: AsyncSession) -> PriceCatalog:
    row = (
        await db.execute(
            select(PriceCatalogVersionRow).where(PriceCatalogVersionRow.is_current.is_(True))
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError("价格目录尚未初始化，请执行数据库迁移")
    return _catalog(row)


async def price_catalog_for_run(db: AsyncSession, run_id: UUID) -> PriceCatalog:
    """首次启动时固定版本；同一 run 恢复时只读已固定的历史版本。"""
    run = (
        await db.execute(select(RunRow).where(RunRow.id == run_id).with_for_update())
    ).scalar_one()
    if run.price_catalog_version is None:
        previous_call = (
            await db.execute(
                select(ModelCallRow.model_call_id).where(ModelCallRow.run_id == run_id).limit(1)
            )
        ).scalar_one_or_none()
        if previous_call is not None:
            raise RuntimeError("旧运行已有模型调用但缺少可恢复的价格版本，拒绝改用当前价格")
        catalog = await current_price_catalog(db)
        run.price_catalog_version = catalog.version
        await db.flush()
        return catalog
    row = (
        await db.execute(
            select(PriceCatalogVersionRow).where(
                PriceCatalogVersionRow.version == run.price_catalog_version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError("运行绑定的价格目录版本不存在，不能改用当前价格")
    return _catalog(row)


async def update_price_catalog(db: AsyncSession, update: PriceUpdate) -> PriceCatalog:
    row = (
        await db.execute(
            select(PriceCatalogVersionRow)
            .where(PriceCatalogVersionRow.is_current.is_(True))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.CONFLICT, "价格目录版本已变化，请刷新后重试")
    if row.version != update.expected_version:
        raise ApiError(ErrorCode.CONFLICT, "价格目录版本已变化，请刷新后重试")

    price = update.to_model_price()
    prices = [
        item
        for item in _catalog(row).prices
        if (item.provider_id, item.model_name) != (price.provider_id, price.model_name)
    ]
    prices.append(price)
    catalog = PriceCatalog(version=uuid4().hex, prices=tuple(prices))
    row.is_current = False
    await db.flush()
    db.add(
        PriceCatalogVersionRow(
            version=catalog.version,
            prices=[item.model_dump(mode="json") for item in catalog.prices],
            is_current=True,
        )
    )
    await db.flush()
    return catalog
