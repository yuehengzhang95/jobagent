# Support assistant (RAG) — project notes

These notes are an example "artifact": a design doc, retro or write-up about work you did.
The Librarian reads every .md / .pdf in artifacts/ and extracts qualitative facts the Tailor
may use for wording. Numbers always come from your resume, never from these notes.

- Retrieval combines BM25 and dense embeddings; results are merged with reciprocal rank fusion.
- Articles are chunked by heading so each chunk answers one question.
- The LLM judge scores faithfulness against a written rubric; disagreements with human labels
  were reviewed weekly to refine the rubric.
- Prompt changes ship only after the judge-based regression suite passes.
