{% if rework %}
Rework ticket {{ key }}. It came back for changes. Address every feedback item, then
update the existing pull request.
{% else %}
Implement ticket {{ key }}.
{% endif %}

- Branch: `{{ branch }}` (already checked out at /workspace)
{% if pr_url %}
- Existing pull request: {{ pr_url }} (push to the same branch; do not open another)
{% endif %}
{% if not local %}
- You can read the ticket and its comments with the get_ticket and get_comments tools, and
  comment with add_comment (only on {{ key }}).
{% endif %}

<ticket>
{{ ticket_md }}
</ticket>
{% if feedback %}

Feedback since the last run, oldest first:

<feedback>
{% for item in feedback %}
- {{ item }}
{% endfor %}
</feedback>
{% endif %}

Definition of done:
1. Implement every acceptance criterion.
2. Add or update unit tests for each criterion, and Playwright tests for criteria a user
   sees in the browser. Each new test must fail without your change.
3. Run lint, type check, the full unit suite and the relevant Playwright specs until green.
4. Commit with messages `{{ key }}: ...`.
{% if local %}
5. Do not push. Print the RESULT line.
{% else %}
5. Push the branch and open the pull request with the template (or update the existing
   one), then print the RESULT line.
{% endif %}
