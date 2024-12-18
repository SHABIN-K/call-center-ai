import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from azure.cosmos import ConsistencyLevel
from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from pydantic import ValidationError

from app.helpers.config_models.database import CosmosDbModel
from app.helpers.features import callback_timeout_hour
from app.helpers.http import azure_transport
from app.helpers.identity import credential
from app.helpers.logging import logger
from app.models.call import CallStateModel
from app.models.readiness import ReadinessEnum
from app.persistence.icache import ICache
from app.persistence.istore import IStore


class CosmosDbStore(IStore):
    _client: CosmosClient | None = None
    _config: CosmosDbModel

    def __init__(self, cache: ICache, config: CosmosDbModel):
        super().__init__(cache)
        logger.info("Using Cosmos DB %s/%s", config.database, config.container)
        self._config = config

    async def areadiness(self) -> ReadinessEnum:
        """
        Check the readiness of the Cosmos DB service.

        This will validate the ACID properties of the database: Create, Read, Update, Delete.
        """
        test_id = str(uuid4())
        test_partition = "+33612345678"
        test_dict = {
            "id": test_id,  # unique id
            "initiate": {
                "phone_number": test_partition,  # partition key
            },
            "test": "test",
        }
        try:
            # Test the item does not exist
            if await self._item_exists(test_id, test_partition):
                return ReadinessEnum.FAIL
            async with self._use_client() as db:
                # Create a new item
                await db.upsert_item(body=test_dict)
                # Test the item is the same
                read_item = await db.read_item(
                    item=test_id, partition_key=test_partition
                )
                assert (
                    {k: v for k, v in read_item.items() if k in test_dict} == test_dict
                )  # Check only the relevant fields, Cosmos DB adds metadata
                # Delete the item
                await db.delete_item(item=test_id, partition_key=test_partition)
            # Test the item does not exist
            if await self._item_exists(test_id, test_partition):
                return ReadinessEnum.FAIL
            return ReadinessEnum.OK
        except AssertionError:
            logger.exception("Readiness test failed")
        except CosmosHttpResponseError:
            logger.exception("Error requesting CosmosDB")
        except Exception:
            logger.error(
                "Unknown error while checking Cosmos DB readiness", exc_info=True
            )
        return ReadinessEnum.FAIL

    async def _item_exists(self, test_id: str, partition_key: str) -> bool:
        exist = False
        async with self._use_client() as db:
            try:
                await db.read_item(item=test_id, partition_key=partition_key)
                exist = True
            except CosmosResourceNotFoundError:
                pass
        return exist

    async def call_aget(self, call_id: UUID) -> CallStateModel | None:
        logger.debug("Loading call %s", call_id)

        # Try cache
        cache_key = self._cache_key_call_id(call_id)
        cached = await self._cache.aget(cache_key)
        if cached:
            try:
                return CallStateModel.model_validate_json(cached)
            except ValidationError as e:
                logger.debug("Parsing error: %s", e.errors())

        # Try live
        call = None
        try:
            async with self._use_client() as db:
                items = db.query_items(
                    query="SELECT * FROM c WHERE STRINGEQUALS(c.id, @id)",
                    parameters=[{"name": "@id", "value": str(call_id)}],
                )
                raw = await anext(items)
                try:
                    call = CallStateModel.model_validate(raw)
                except ValidationError as e:
                    logger.debug("Parsing error: %s", e.errors())
        except StopAsyncIteration:
            pass
        except CosmosHttpResponseError as e:
            logger.error("Error accessing CosmosDB: %s", e)

        # Update cache
        if call:
            await self._cache.aset(
                key=cache_key,
                ttl_sec=await callback_timeout_hour(),
                value=call.model_dump_json(),
            )

        return call

    async def call_aset(self, call: CallStateModel) -> bool:
        logger.debug("Saving call %s", call.call_id)

        # Update live
        data = call.model_dump(mode="json", exclude_none=True)
        data["id"] = str(call.call_id)  # CosmosDB requires an id field
        res = False
        try:
            async with self._use_client() as db:
                await db.upsert_item(body=data)
            res = True
        except CosmosHttpResponseError as e:
            logger.error("Error accessing CosmosDB: %s", e)

        # Update cache
        if res:
            cache_key_id = self._cache_key_call_id(call.call_id)
            await self._cache.aset(
                key=cache_key_id,
                ttl_sec=await callback_timeout_hour(),
                value=call.model_dump_json(),
            )  # Update for ID
            cache_key_phone_number = self._cache_key_phone_number(
                call.initiate.phone_number
            )
            await self._cache.adel(
                cache_key_phone_number
            )  # Invalidate for phone number because we don't know if it's the same call

        return res

    async def call_asearch_one(self, phone_number: str) -> CallStateModel | None:
        logger.debug("Loading last call for %s", phone_number)

        # Try cache
        cache_key = self._cache_key_phone_number(phone_number)
        cached = await self._cache.aget(cache_key)
        if cached:
            try:
                return CallStateModel.model_validate_json(cached)
            except ValidationError:
                logger.debug("Parsing error", exc_info=True)

        # Try live
        call = None
        try:
            async with self._use_client() as db:
                items = db.query_items(
                    max_item_count=1,
                    query=f"SELECT * FROM c WHERE (STRINGEQUALS(c.initiate.phone_number, @phone_number, true) OR STRINGEQUALS(c.claim.policyholder_phone, @phone_number, true)) AND c.created_at >= DATETIMEADD('hh', -{await callback_timeout_hour()}, GETCURRENTDATETIME()) ORDER BY c.created_at DESC",
                    parameters=[
                        {
                            "name": "@phone_number",
                            "value": phone_number,
                        }
                    ],
                )
                raw = await anext(items)
                try:
                    call = CallStateModel.model_validate(raw)
                except ValidationError:
                    logger.debug("Parsing error", exc_info=True)
        except StopAsyncIteration:
            pass
        except CosmosHttpResponseError:
            logger.exception("Error accessing CosmosDB")

        # Update cache
        if call:
            await self._cache.aset(
                key=cache_key,
                ttl_sec=await callback_timeout_hour(),
                value=call.model_dump_json(),
            )

        return call

    async def call_asearch_all(
        self,
        count: int,
        phone_number: str | None = None,
    ) -> tuple[list[CallStateModel] | None, int]:
        logger.debug("Searching calls, for %s and count %s", phone_number, count)
        # TODO: Cache results
        calls, total = await asyncio.gather(
            self._call_asearch_all_calls_worker(count, phone_number),
            self._call_asearch_all_total_worker(phone_number),
        )
        return calls, total

    async def _call_asearch_all_calls_worker(
        self,
        count: int,
        phone_number: str | None = None,
    ) -> list[CallStateModel] | None:
        calls: list[CallStateModel] = []
        try:
            async with self._use_client() as db:
                where_clause = (
                    "WHERE STRINGEQUALS(c.initiate.phone_number, @phone_number, true) OR STRINGEQUALS(c.claim.policyholder_phone, @phone_number, true)"
                    if phone_number
                    else ""
                )
                items = db.query_items(
                    query=f"SELECT * FROM c {where_clause} ORDER BY c.created_at DESC OFFSET 0 LIMIT @count",
                    parameters=[
                        {
                            "name": "@phone_number",
                            "value": phone_number,
                        },
                        {
                            "name": "@count",
                            "value": count,
                        },
                    ],
                )
                async for raw in items:
                    if not raw:
                        continue
                    try:
                        calls.append(CallStateModel.model_validate(raw))
                    except ValidationError:
                        logger.debug("Parsing error", exc_info=True)
        except CosmosHttpResponseError:
            logger.exception("Error accessing CosmosDB")
        return calls

    async def _call_asearch_all_total_worker(
        self,
        phone_number: str | None = None,
    ) -> int:
        total = 0
        try:
            async with self._use_client() as db:
                where_clause = (
                    "WHERE STRINGEQUALS(c.initiate.phone_number, @phone_number, true) OR STRINGEQUALS(c.claim.policyholder_phone, @phone_number, true)"
                    if phone_number
                    else ""
                )
                items = db.query_items(
                    query=f"SELECT VALUE COUNT(1) FROM c {where_clause}",
                    parameters=[
                        {
                            "name": "@phone_number",
                            "value": phone_number,
                        },
                    ],
                )
                total: int = await anext(items)  # pyright: ignore
        except CosmosHttpResponseError:
            logger.exception("Error accessing CosmosDB")

        return total

    @asynccontextmanager
    async def _use_client(self) -> AsyncGenerator[ContainerProxy, None]:
        """
        Generate the Cosmos DB client and close it after use.
        """
        if not self._client:
            self._client = CosmosClient(
                # Usage
                consistency_level=ConsistencyLevel.Eventual,
                # Reliability
                connection_timeout=10,  # 10 secs
                retry_backoff_factor=0.8,
                retry_backoff_max=8,
                retry_total=3,
                # Performance
                transport=await azure_transport(),
                # Deployment
                url=self._config.endpoint,
                # Authentication
                credential=await credential(),
            )
        async with self._client as client:
            database = client.get_database_client(self._config.database)
            yield database.get_container_client(self._config.container)
