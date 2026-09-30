Tickets merged since the last docs run ({{ tickets | length }}):

<tickets>
{% for t in tickets %}
## {{ t.key }}: {{ t.summary }}
Epic: {{ t.epic or "none" }}. Review loops: {{ t.review_loop }}. Human returns: {{ t.human_returns }}.

Ticket description:
{{ t.description }}

Pull request {{ t.pr_title }}:
{{ t.pr_body }}

Reviewer summary:
{{ t.review_summary or "none" }}

{% endfor %}
</tickets>
