#!/usr/bin/env python3
"""
Скрипт для пересоздания векторной базы данных ChromaDB из SQLite

Использование:
    python rebuild_chromadb.py [--force] [--db-path PATH] [--chroma-path PATH] [--model MODEL]

Параметры:
    --force           Принудительно пересоздать ChromaDB (по умолчанию)
    --db-path         Путь к SQLite базе данных (по умолчанию: smolagents_memory.db)
    --chroma-path     Путь к директории ChromaDB (по умолчанию: smolagents_chroma_db)
    --model           Модель эмбеддингов (по умолчанию: all-MiniLM-L6-v2)
    --help, -h        Показать справку

Примеры:
    python rebuild_chromadb.py
    python rebuild_chromadb.py --db-path custom_memory.db
    python rebuild_chromadb.py --model sentence-transformers/all-mpnet-base-v2
"""

import argparse
import sys
import os
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(
        description="Пересоздание векторной базы данных ChromaDB из SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--force", 
        action="store_true", 
        default=True,
        help="Принудительно пересоздать ChromaDB (по умолчанию: включено)"
    )
    
    parser.add_argument(
        "--db-path",
        default="smolagents_memory.db",
        help="Путь к SQLite базе данных (по умолчанию: smolagents_memory.db)"
    )
    
    parser.add_argument(
        "--chroma-path", 
        default="smolagents_chroma_db",
        help="Путь к директории ChromaDB (по умолчанию: smolagents_chroma_db)"
    )
    
    parser.add_argument(
        "--model",
        default="all-MiniLM-L6-v2", 
        help="Модель эмбеддингов (по умолчанию: all-MiniLM-L6-v2)"
    )
    
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Только показать статистику, не пересоздавать"
    )
    
    args = parser.parse_args()
    
    # Проверяем существование SQLite базы
    if not os.path.exists(args.db_path):
        print(f"❌ Ошибка: SQLite база данных не найдена: {args.db_path}")
        print(f"Убедитесь, что файл существует или укажите правильный путь с --db-path")
        sys.exit(1)
    
    print("🚀 Запуск пересоздания ChromaDB...")
    print(f"📁 SQLite база: {args.db_path}")
    print(f"📁 ChromaDB директория: {args.chroma_path}")
    print(f"🤖 Модель эмбеддингов: {args.model}")
    print()
    
    try:
        # Импортируем после проверок, чтобы избежать долгой загрузки при ошибках
        from memory.database import DatabaseHandler
        from memory.manager import MemoryManager
        from memory.rebuild import rebuild_chromadb_tool
        
        if args.stats_only:
            # Показываем только статистику без пересоздания
            print("📊 Получение текущей статистики...")
            db_handler = DatabaseHandler(
                db_path=args.db_path,
                chroma_path=args.chroma_path,
                embedding_model=args.model
            )
            manager = MemoryManager(database_handler=db_handler)
            
            # Статистика SQLite
            import sqlite3
            conn = sqlite3.connect(args.db_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM agent_memory")
            tactical_sqlite = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM strategic_memory")
            strategic_sqlite = cursor.fetchone()[0]
            
            conn.close()
            
            # Статистика ChromaDB
            tactical_chroma = manager.tactical_collection.count() if manager.tactical_collection else 0
            strategic_chroma = manager.strategic_collection.count() if manager.strategic_collection else 0
            
            print(f"📋 Тактическая память:")
            print(f"   SQLite: {tactical_sqlite} записей")
            print(f"   ChromaDB: {tactical_chroma} записей")
            print(f"   Синхронизация: {'✅' if tactical_sqlite == tactical_chroma else '❌'}")
            
            print(f"🎯 Стратегическая память:")
            print(f"   SQLite: {strategic_sqlite} записей") 
            print(f"   ChromaDB: {strategic_chroma} записей")
            print(f"   Синхронизация: {'✅' if strategic_sqlite == strategic_chroma else '❌'}")
            
        else:
            # Пересоздаем ChromaDB
            if args.force:
                # Используем force_rebuild параметр
                db_handler = DatabaseHandler(
                    db_path=args.db_path,
                    chroma_path=args.chroma_path,
                    embedding_model=args.model
                )
                manager = MemoryManager(database_handler=db_handler, force_rebuild=True)
            else:
                # Используем функцию rebuild_chromadb_tool
                db_handler = DatabaseHandler(
                    db_path=args.db_path,
                    chroma_path=args.chroma_path,
                    embedding_model=args.model
                )
                manager = MemoryManager(database_handler=db_handler, force_rebuild=False)
                result = rebuild_chromadb_tool()
                print(result)
    
    except KeyboardInterrupt:
        print("\n🛑 Операция прервана пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main() 