"""Initialize application tables, LangGraph checkpoint tables, and Kafka topics.

Run before gateway and worker services to prepare shared infrastructure. Existing
topics are tolerated. The local defaults of three partitions and one broker replica
do not set the number of worker processes.
"""

import asyncio

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from incident_platform.config import settings
from incident_platform.storage import Store
from incident_platform.transport import TOPICS


async def main():
    cfg = settings()
    store = Store(cfg.database_url)
    await store.open()
    try:
        await store.setup()
        async with AsyncPostgresSaver.from_conn_string(cfg.database_url) as saver:
            await saver.setup()
        admin = AIOKafkaAdminClient(bootstrap_servers=cfg.kafka_bootstrap_servers)
        await admin.start()
        try:
            for name in TOPICS:
                try:
                    await admin.create_topics(
                        [NewTopic(name, num_partitions=3, replication_factor=1)]
                    )
                except TopicAlreadyExistsError:
                    pass
        finally:
            await admin.close()
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
