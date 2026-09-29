# templates

Jinja2 templates for text CodeIt writes, as opposed to prompts it sends to models.

| Path | Used for |
|---|---|
| `planner/epic.md`, `planner/story.md` | Jira descriptions of tickets the Planner creates |
| `pr_body.md` | The pull request template (embedded in the `open-pr` skill) |
| `target/` | The steering kit `codeit init-target` copies into a target repo. `.j2` files are rendered with the repo's `codeit.yaml`. |

The skills name this stack's test tools (Vitest, supertest, Testing Library, Playwright). For another stack, edit them after `init-target`.
