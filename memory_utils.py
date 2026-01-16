import json
import os
import time
import logging
from pathlib import Path

class MemoryManager:
    def __init__(self, base_path="accounts"):
        self.base_path = Path(base_path)
        self.memory_file = self.base_path / "memory.json"
        self.logger = logging.getLogger("MemoryManager")
        self.memories = self.load_memory()

    def load_memory(self):
        """Load memories from JSON file."""
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.logger.error(f"Error loading memory: {e}")
                return []
        return []

    def save_memory(self):
        """Save memories to JSON file."""
        try:
            temp_file = str(self.memory_file) + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, self.memory_file)
        except Exception as e:
            self.logger.error(f"Error saving memory: {e}")

    def add_memory(self, user_id, text, role):
        """Save a new memory (message) for a user."""
        try:
            memory_item = {
                "user_id": str(user_id),
                "text": text,
                "role": role,
                "timestamp": time.time()
            }
            self.memories.append(memory_item)
            # Optimize: Keep last 1000 messages total or per user? 
            # For now, let's keep it simple. If it grows too big, we can prune.
            if len(self.memories) > 5000:
                self.memories = self.memories[-5000:]
            
            self.save_memory()
        except Exception as e:
            self.logger.error(f"Error adding memory: {e}")

    def get_context(self, user_id, query, limit=5):
        """Retrieve relevant past interactions."""
        try:
            user_id = str(user_id)
            # Filter memories for this user
            user_memories = [m for m in self.memories if m.get("user_id") == user_id]
            
            if not user_memories:
                return ""

            # 1. Always get recent short-term context (last 3 messages)
            # Note: main.py already gets history from Telethon-based file, 
            # but memory.json acts as long-term storage.
            
            # Simple keyword matching for "Long-Term" recall
            # Exclude very recent messages to avoid duplication with short-term history
            # (Assuming query is the current user message)
            
            relevant_memories = []
            keywords = [w.lower() for w in query.split() if len(w) > 3] # simple filter
            
            # Search in older messages (everything except last 5)
            older_memories = user_memories[:-5] 
            
            for mem in older_memories:
                mem_text = mem.get("text", "").lower()
                score = sum(1 for k in keywords if k in mem_text)
                if score > 0:
                    relevant_memories.append((score, mem))
            
            # Sort by score (desc), then timestamp (desc)
            relevant_memories.sort(key=lambda x: (x[0], x[1]['timestamp']), reverse=True)
            
            # Take top 'limit' matches
            top_memories = [m[1] for m in relevant_memories[:limit]]
            
            # Sort chronologically for readable context
            top_memories.sort(key=lambda x: x['timestamp'])

            formatted_context = []
            for mem in top_memories:
                role = mem.get("role", "unknown")
                text = mem.get("text", "").replace("\n", " ")
                # Convert timestamp to readable date
                ts = mem.get("timestamp", 0)
                date_str = time.strftime('%Y-%m-%d', time.localtime(ts))
                formatted_context.append(f"[{date_str}] {role}: {text}")
            
            return "\n".join(formatted_context)
            
        except Exception as e:
            self.logger.error(f"Error retrieving memory: {e}")
            return ""
