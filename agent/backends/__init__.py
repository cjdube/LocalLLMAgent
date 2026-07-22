"""LLM backend adapters behind agent.loop's _llm_chat seam.

Each backend translates Wren's one canonical message/tool shape to and from its
provider's format internally, so agent.loop's advance()/complete_text() and
their callers speak a single shape regardless of provider. The default is local
Ollama (in agent.loop); a cloud backend is opt-in per the local-first design.
"""
