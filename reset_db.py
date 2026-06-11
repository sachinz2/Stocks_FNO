import asyncio
import aiomysql

async def reset_database():
    print("Connecting to MySQL to reset the database...")
    try:
        conn = await aiomysql.connect(host='127.0.0.1', port=3307, user='root', password='password123')
        async with conn.cursor() as cur:
            print("Dropping falcon_db if it exists...")
            await cur.execute("DROP DATABASE IF EXISTS falcon_db;")
            print("Creating fresh falcon_db...")
            await cur.execute("CREATE DATABASE falcon_db;")
        conn.close()
        print("Database reset successful!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(reset_database())
