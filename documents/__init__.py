"""The Document Architect — real Word, Excel, PowerPoint and PDF deliverables.

``documents.builders`` writes the files (and probes for the optional packages
that do it), ``documents.tool`` is the tool the assistant calls. Nothing here
plans: the model supplies a declarative spec, the builder writes a real file and
reports the path it wrote.
"""

from __future__ import annotations

__all__ = ["builders", "tool"]
