Plan the following work. Reply with one JSON object: an `epic` and its `stories`.

<plan>
{{ plan_md }}
</plan>

{% if claude_md %}
The target repository's CLAUDE.md:

<claude_md>
{{ claude_md }}
</claude_md>

{% endif %}
{% if repo_tree %}
The target repository's files (depth 3):

<repo_tree>
{{ repo_tree }}
</repo_tree>

{% else %}
The target repository is not available locally, so plan from the text alone.

{% endif %}
{% if open_tickets %}
Tickets already open in Jira:

<open_tickets>
{% for line in open_tickets %}
{{ line }}
{% endfor %}
</open_tickets>
{% else %}
There are no open tickets in Jira.
{% endif %}
