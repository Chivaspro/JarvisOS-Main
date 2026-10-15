# knowledge-retrieval Specification

## Purpose

Gives JARVIS an optional searchable knowledge corpus — documents, PDFs, notes and
project documentation — via a Qdrant-backed vector store with a local fallback,
distinct from personal long-term memory: Mem0 remembers the user, the knowledge
layer answers about documents.

## Requirements

### Requirement: Knowledge retrieval is separate from personal memory
The system SHALL keep the knowledge corpus (documents, PDFs, notes, project
documentation) in a dedicated knowledge store, separate from long-term personal
memory, and project/file search tools SHALL continue to work without any vector
store configured.

#### Scenario: No vector store configured
- **WHEN** the user searches project documents with default configuration
- **THEN** the existing local file-search behavior serves the request unchanged

### Requirement: Optional Qdrant backend
The system SHALL support Qdrant as an optional knowledge backend, enabled only
when configured; when enabled, ingested documents SHALL be searchable through
semantic queries with source citations back to the original files.

#### Scenario: Qdrant enabled
- **WHEN** the Qdrant backend is configured and documents are ingested
- **THEN** semantic queries return relevant passages with their source file paths

### Requirement: Graceful degradation
If the knowledge backend is unavailable or errors, the system SHALL fall back to
local search, log the degradation once, and never fail the user's request.

#### Scenario: Backend down
- **WHEN** a knowledge query runs while the vector store is unreachable
- **THEN** the local search fallback serves the request and a single degradation notice is logged
