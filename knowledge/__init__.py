"""Mark LII knowledge layer (change: modernize-jarvis-architecture).

A searchable knowledge corpus — documents, PDFs, notes, project docs —
distinct from personal long-term memory: Mem0 remembers the user, the
knowledge layer answers about documents. Default path is local file search
(no vector store); Qdrant is an optional backend behind
``features.qdrant_knowledge`` with graceful fallback.
"""

from knowledge.retriever import knowledge_search, knowledge_check, knowledge_status

__all__ = ["knowledge_search", "knowledge_check", "knowledge_status"]
