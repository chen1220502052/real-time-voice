"""Import versioned unit words, cumulative snapshots and reviewed replies into SQLite."""
import json
from backend.chat_library import chat_library

if __name__ == '__main__':
    library = chat_library()
    print(json.dumps({'database':str(library.path),**library.stats()},ensure_ascii=False))
