import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

uri = os.getenv("MONGO_URI")

if not uri or "mongodb+srv://" not in uri:
    raise ValueError("MONGO_URI missing or not configured for Atlas")

client = MongoClient(uri, serverSelectionTimeoutMS=15000)

try:
    print(client.admin.command("ping"))
    print("MongoDB connected successfully!")
except Exception as e:
    print(type(e).__name__, e)
finally:
    client.close()