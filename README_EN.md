English | [Русский](README.md)

# MultiAgent

Advanced multi-agent system for solving complex tasks using specialized AI agents. Implements modern architectural patterns with an emphasis on modularity, security, and extensibility.

## 🏗️ Architectural Features

### **Agent-Based Architecture** 
Hierarchical agent system with clear separation of responsibilities:
- **Agent Factory** for dynamic creation of specialized agents
- **Manager Agent** for coordinating team operations
- **Flexible Pipelines** for various types of tasks (Text-to-SQL, research, content creation)

### **Database Plugin Pattern**
Unified interface for working with various DBMS:
- SQLite, PostgreSQL, MySQL, DuckDB, SAP IQ, Impala
- Dialect-specific SQL generation
- Safe execution with read-only connections

### **Next-Gen RAG Memory**
Hybrid memory system with advanced capabilities:
- **SQLite + ChromaDB** for structured and semantic search
- **Access policies** at agent level (agent/session/strategic scope)
- **Automatic summarization** via LLM for long-term memory
- **Contextual enrichment** based on semantic search

## Quick Start

### Install Dependencies
```bash
# Install Mermaid CLI for diagrams
npm install -g @mermaid-js/mermaid-cli

# Install Python dependencies
pip install -r requirements.txt

# Activate the virtual environment
source .venv/bin/activate
```

### Configuration
```bash
# Set environment variables
export OPENAI_API_KEY_DB="your-api-key"
export OPENAI_API_BASE_DB="your-api-base"
export DB_DSN="sqlite:///path/to/your.db"  # Optional for Text-to-SQL
```

## Documentation

### Core Components
- [**Text-to-SQL System**](doc/TEXT_TO_SQL.md) - Natural language to SQL query conversion
- [**RAG Memory Configuration**](doc/RAG_MEMORY_CONFIGURATION.md) - Configuring agent memory system
- [**Mermaid Diagram Integration**](doc/MERMAID_INTEGRATION_SUMMARY.md) - Creating diagrams

### Setup and Configuration
- [**Custom Agent Response Templates**](doc/CUSTOM_RESPONSE_TEMPLATES.md) - Managing agent output format
- [**ChromaDB Rebuild Guide**](doc/CHROMADB_REBUILD_GUIDE.md) - Vector database maintenance
- [**Release Notes**](doc/RELEASE_NOTES.md) - Change history and new features

### Development Plans
- [**🛣️ Development Roadmap**](doc/DEVELOPMENT_ROADMAP.md) - Strategic plans based on SOTA solutions

### Enterprise Functionality
- [**⚡ Workflow Engine**](doc/WORKFLOW_ENGINE.md) - Reliable workflow execution system

## 🚀 Key Features

### 🎯 Response Format Management
Flexible configuration of agent output format:
- **Clean JSON** without wrapper text
- **Custom templates** for specific formats  
- **Full backward compatibility** with existing agents

```yaml
# Example configuration in agent profile
custom_report_template: "{{final_answer}}"
```

### 🧠 Multi-Agent Architecture
**Three main execution pipelines:**

1. **Text-to-SQL Pipeline**: `Manager → NLU → Schema RAG → SQL Generator → SQL Verifier → DB Audit`
2. **Educational Content**: `Manager → Researcher → Analyst → Course Plan → Content Expert → Lab Designer`
3. **General Tasks**: `Manager → Researcher → [Specialized Agents]`

### 📊 Text-to-SQL Engine
Advanced pipeline for database operations:
- **NLU analysis** of natural language queries
- **Schema RAG** for semantic binding with DB schema
- **Safe SQL generation** with multiple validation
- **Multi-level audit** of query execution

### 🛡️ Security System
Multi-layer protection at all levels:
- **LLM-Guard** for filtering incoming requests
- **SQL validation** against injections and unsafe operations
- **PII scanning** for personal data protection
- **Sandbox** for safe code execution

### 🔧 Extensible Tool System
Modular tool architecture:
- **YAML configurations** for declarative description
- **MCP integration** for connecting external services
- **Plugin system** for adding new capabilities

### ⚡ Workflow Engine (Enterprise)
Reliable workflow execution system:
- **State persistence** - recovery after failures
- **Retry mechanisms** - automatic retries on errors
- **Resource management** - client isolation and quotas
- **Checkpointing** - execution progress persistence
- **Full compatibility** - non-breaking extension

## 📈 Architectural Advantages

- **Modularity**: Independent components with clear interfaces
- **Scalability**: Easy addition of new agents and tools  
- **Security**: Multi-level validation and access control
- **Observability**: Detailed logging and HTML process visualization
- **Performance**: Asynchronous operations and optimized queries
