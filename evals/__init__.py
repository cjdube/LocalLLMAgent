"""Model bake-off harness: run Wren's own prompts against candidate local
models and score what comes back.

Deliberately thin. It drives the SAME code paths production uses —
agent.loop.advance() for chat and agent.loop.complete_text() for the scheduled
tasks — rather than talking to Ollama itself, so a result here is a statement
about Wren and not about a parallel client with different settings. See
docs/model-eval.md.
"""
