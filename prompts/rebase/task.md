Finish rebasing branch `{{ branch }}` for ticket {{ key }} onto `{{ base }}`.

The rebase stopped with conflicts in:
{% for f in conflicts %}
- `{{ f }}`
{% endfor %}

What this ticket changes:

<ticket>
{{ ticket_md }}
</ticket>

What landed on main since this branch was made (newest first):

<main_changes>
{% for c in main_changes %}
- {{ c }}
{% endfor %}
</main_changes>
