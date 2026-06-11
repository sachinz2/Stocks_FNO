import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Database Configuration (Assuming async MySQL driver like aiomysql or asyncmy)
# Example format: mysql+aiomysql://user:pass@localhost:3306/falcon_db
DB_URL = os.getenv("DATABASE_URL", "mysql+aiomysql://root:password@localhost:3306/falcon_db")

# Create Async Engine
engine = create_async_engine(
    DB_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)

# Async Session Maker
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db_session() -> AsyncSession:
    """Dependency to provide a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()