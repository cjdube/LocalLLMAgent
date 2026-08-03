# Sharing one Ollama

Wren does not have Ollama to herself. One instance on the Mac mini
(`gemma4:26b-mlx`, ~27 GB resident of 48 GB) serves **four** independent
consumers:

| Consumer | What it is |
|---|---|
| Wren chat | `chat/server.py`, interactive, someone waiting on a phone |
| Wren's scheduled tasks | ~12 launchd jobs, staggered across the day |
| `wiki_ingest.py` | ObsidianWikiAgent, launchd `com.craigdube.wikiagent.learnings-ingest`, daily 9:00 AM |
| WeighAnchor | game AI, `PROVIDER_DEFAULT=ollama`, node server on `:3002` |

Ollama runs with **`OLLAMA_NUM_PARALLEL=1`** and `OLLAMA_MAX_QUEUE=512`: one
request at a time, everything else queued **silently** — the socket is
accepted and no bytes flow.

That last detail is the whole problem. A queued request and a dead server look
identical from the client: nothing on the wire either way. Wren streams, so her
read timeout is a *between-chunks* timeout, which cannot tell them apart on its
own.

## Two failure modes that look the same

**Starvation.** A long job holds the slot; everyone else waits. Ollama is
healthy. Cured by waiting, or by stopping the job holding the slot.

**Runner wedge.** The MLX runner accepts a request, completes prefill, then
never generates. Ollama's HTTP layer is healthy and answers `/api/ps`
throughout. **Only killing the runner clears it.**

They are worth separating because the remedies differ, and because the second
one will otherwise be misread as the first ("something big must still be
running") for as long as you are willing to wait.

### Telling them apart

Probe Ollama **directly**, bypassing Wren:

```bash
curl -s -m 60 http://localhost:11434/api/chat -d '{"model":"gemma4:26b-mlx","messages":[{"role":"user","content":"Say OK."}],"stream":false,"think":false,"options":{"num_predict":8}}'
```

| Signal | Starved | Wedged |
|---|---|---|
| A 16-token request | eventually answers | **zero bytes, indefinitely** |
| `logs/server.log` `Prompt processing progress` | advancing | **absent since the stall** |
| `/v1/status` latency | ~40µs | **~50ms** |
| Runner process CPU | working | **spinning, completing nothing** |
| `/api/ps` | model resident | model resident (identical — not a signal) |

`/api/ps` looks fine in both cases. It tells you Ollama is *up*, which is what
`_diagnose_stall` uses it for; it does not tell you the runner is *working*.

### Clearing a wedge

Check what you're about to kill first — it should be exactly one process, the
runner, never the `ollama` server itself:

```bash
pgrep -laf "runner --mlx-engine"
```

```bash
pkill -f "runner --mlx-engine"
```

The `ollama` server process respawns the runner and reloads the model on the
next request (~5s). Restarting the whole Ollama app is not necessary.

## What Wren does about it

`agent/loop.py:_ollama_chat` catches the transport failure, probes `/api/ps`,
and raises `OllamaUnavailable` with a message naming the actual cause:

> Ollama at http://localhost:11434 is up (model gemma4:26b-mlx loaded) but
> stalled without producing any output after 118s. It serves one request at a
> time, so it is either busy with another job or its runner is wedged. Retry;
> if it repeats, restart Ollama.

versus, when the probe itself fails:

> Ollama at http://localhost:1 did not respond within 3s and is not answering
> status checks either — it looks down. Check that it is running.

It also distinguishes a stream that **never started** (queued, or wedged) from
one that **died mid-reply** — different faults that previously reported the
same thing.

Interactive turns use `WREN_CHAT_MODEL_TIMEOUT` (120s) rather than the
scheduled tasks' `OLLAMA_TIMEOUT` (300s). Because it is a between-chunks
timeout it only has to cover the wait for the *first* token; cold prefill of a
40k-token prompt measured ~50s, so 120s is generous. Past that, a person
waiting is better served by a fast accurate answer than a long spinner.

## Incident: 2026-08-03

`wiki_ingest` started at 09:00:04 and was still running at 11:54 — **2h54m**.
The runner wedged at ~09:02:30, while `wiki_ingest` was the *only* consumer
running (Wren's chat server didn't restart until 09:14:49). So contention did
not cause the wedge; it only determined who noticed.

`wiki_ingest` then retried with exponential backoff — 2.9s, 4.6s, 8.6s, 17.0s —
for nearly three hours, each retry re-occupying the single slot. Wren's turns
in that window queued behind it and reported a bare `ReadTimeout`. A chat turn
of `2 messages, 24 tools` sent at 11:38:12 failed at 11:43:12 having received
nothing.

Measured on the wedged runner (up 7h26m at that point):

| Probe | Result |
|---|---|
| 900-token generation | zero bytes in 180s |
| 16-token generation | zero bytes in 90s |
| 4-token generation | zero bytes in 40s |
| Runner CPU | +5 min over 20 min wall, zero completions |

Killing the runner restored service in 5.8s (5.2s of it model reload).

### What was ruled out

**Do not spend time re-running these.** None of them reproduce the wedge on
demand:

| Hypothesis | Test | Result |
|---|---|---|
| Prompt size | 2.7k → 40k tokens, non-streamed | all `done_reason: stop`, including past `num_ctx` |
| Tool calling | 29k-token prompt with tool schemas | fine |
| Thinking | `think` on and off at 29k | fine |
| Streaming | 16k–23k tokens, `stream: true` | fine |

The trigger is not prompt size, tool use, thinking, or streaming. Note that the
client that *did* wedge it (`wiki_ingest`) uses `stream: false`.

### Upstream

Closest match is [ml-explore/mlx-lm#1493](https://github.com/ml-explore/mlx-lm/issues/1493):
Gemma-4-26B, 22–26k-token prompts, hang immediately after prefill, and —
matching our experience exactly — *only real client traffic reproduces it;
synthetic requests of the same size complete fine*. Our last successful prompt
before the wedge was 22,457 tokens and the stuck retry was 26,901, inside that
band.

Two honest mismatches: #1493 reports 0% CPU where ours spun (closer to the
busy-poll in [#1500](https://github.com/ml-explore/mlx-lm/issues/1500)), and its
client streamed where ours did not. Ollama's `--mlx-engine` is also its own
integration, not `mlx_lm.server`. So: same family of defect, not a proven
identical bug. Unresolved upstream as of Ollama 0.32.5.

**Treat it as detect-and-restart, not fix.**

## If this keeps happening

Options, roughly in order of value:

- **A watchdog** that recognises the wedge signature above and kills the runner.
  Nothing has been built.
- **Raise `OLLAMA_NUM_PARALLEL`** so a background job cannot starve chat. Each
  slot costs KV-cache memory, and the box already runs a 27 GB model in 48 GB
  with ~2.6 GB of swap in use — measure before committing.
- **A run budget in the other projects' jobs**, so a stalled model server makes
  them exit rather than retry for hours.

## Related

- [docs/llm-backend.md](llm-backend.md) — the `_llm_chat` seam and routing a
  task to a cloud backend, which sidesteps this contention entirely
- [docs/model-constraints.md](model-constraints.md) — failure modes of the
  model's *output*, as opposed to its serving
- [docs/log-inspector.md](log-inspector.md) — what does and doesn't raise an
  alert from the logs
