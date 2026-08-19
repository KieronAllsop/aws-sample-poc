import asyncio
from license_server import Base, engine

async def init_models():
    print("⏳ Connecting to database and generating schemas...")
    async with engine.begin() as conn:
        # Properly run schema generation via the required async worker loop
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Database tables successfully created!")

if __name__ == "__main__":
    asyncio.run(init_models())
