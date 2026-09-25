**User story:** {{ story.user_story }}

## Acceptance criteria

{% for ac in story.acceptance_criteria %}
- [ ] {{ ac }}
{% endfor %}
{% if story.technical_notes_md %}

## Technical notes

{{ story.technical_notes_md }}
{% endif %}
{% if story.test_plan.unit or story.test_plan.e2e %}

## Test plan
{% if story.test_plan.unit %}

**Unit**

{% for t in story.test_plan.unit %}
- {{ t }}
{% endfor %}
{% endif %}
{% if story.test_plan.e2e %}

**End to end (Playwright)**

{% for t in story.test_plan.e2e %}
- {{ t }}
{% endfor %}
{% endif %}
{% endif %}

---

Suggested points: {{ story.suggested_points }}. Risk: {{ story.risk }}. Planner ref {{ story.ref }}, run {{ run_id }}.
