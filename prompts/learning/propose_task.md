The file is `{{ path }}`. It steers the {{ agent }}.

<file>
{{ current }}
</file>

<lessons>
{% for l in lessons %}
## `{{ l.key }}` ({{ l.theme }}): {{ l.lesson }}
Evidence ({{ l.signals | length }}):
{% for s in l.signals %}
- {{ s.text | truncate(400) }}
{% endfor %}

{% endfor %}
</lessons>
