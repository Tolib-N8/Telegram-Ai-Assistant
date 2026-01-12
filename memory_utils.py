import os
import chromadb
from chromadb.config import Settings
import time
import logging

class MemoryManager:
    def __init__(self, base_path="accounts"):
        self.client = chromadb.PersistentClient(path=os.path.join(base_path, "chroma_db"))
        self.collection = self.client.get_or_create_collection(name="user_memories")
        self.logger = logging.getLogger("MemoryManager")

    def add_memory(self, user_id, text, role):
        """Save a new memory (message) for a user."""
        try:
            timestamp = time.time()
            # ID must be unique
            mem_id = f"{user_id}_{int(timestamp*1000)}_{role}"
            
            self.collection.add(
                documents=[text],
                metadatas=[{"user_id": str(user_id), "role": role, "timestamp": timestamp}],
                ids=[mem_id]
            )
        except Exception as e:
            self.logger.error(f"Error adding memory: {e}")

    def get_context(self, user_id, query, limit=5):
        """Retrieve relevant past interactions."""
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=limit,
                where={"user_id": str(user_id)} # Filter by specific user
            )
            
            if not results['documents']:
                return ""

            # Format memories
            context = []
            docs = results['documents'][0]
            metas = results['metadatas'][0]
            
            # Sort by timestamp roughly if possible, but semantic search returns by relevance.
            # actually we might want to just show them as "Relevant Context"
            
            for doc, meta in zip(docs, metas):
                role = meta.get('role', 'unknown')
                context.append(f"[{role}]: {doc}")
            
            return "\n".join(context)
        except Exception as e:
            self.logger.error(f"Error retrieving memory: {e}")
            return ""
