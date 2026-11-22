# Documentation

| Area | Document | What it covers |
|---|---|---|
| Design | [Architecture](design/architecture.md) | Modular monolith, durable worker, state and transaction model, payment recovery, decision boundaries |
| | [Contracts](design/contracts.md) | Canonical Pydantic models, OpenAPI export, generated TypeScript types |
| | [LLM layer](design/llm-layer.md) | Gemini (Vertex AI) stages, structured output, validation, call caps and caching |
| | [Human review](design/human-review.md) | `WAITING_FOR_HUMAN` hand-offs, resolution actions, budget and payment safeguards |
| API | [OpenAPI 3.1 spec](api/openapi.json) | Machine-readable contract; regenerate with `make contracts` |
| Guides | [Demo script](guides/demo.md) | Scenarios for a live walkthrough |
| | [Deployment](guides/deployment.md) | Docker image, Render blueprint, secrets, spend guards |
| | [Live test](guides/live-test.md) | Running against real Crustdata and Vertex AI credentials |
| | [Team CLI](guides/team-cli.md) | The `desk` command-line agent and notebook |
| Quality | [Verification](quality/verification.md) | What has been verified, how, and what has not |
| | [Benchmark](quality/benchmark.md) | Runnable backend benchmark and API coverage |
| | [CLI benchmark](quality/sid-benchmark.md) | Archived results for the CLI branch |
| Planning | [Implementation plan](planning/implementation-plan.md) | Team plan and ownership |
| | [Team notes](planning/team-notes.md) | What we tried, what worked, what did not |
| Brief | [Project brief](brief/sourcing-desk-project-brief.pdf), [Builder handbook](brief/contextcon-05-builder-handbook.pdf) | The hackathon problem statement and rules |
