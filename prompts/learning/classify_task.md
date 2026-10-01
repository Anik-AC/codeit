<existing_lessons>
{% for l in existing %}
- `{{ l.key }}` ({{ l.target }}, {{ l.theme }}): {{ l.lesson }}
{% else %}
(none yet)
{% endfor %}
</existing_lessons>

<signals>
{% for s in signals %}
### Signal {{ s.id }} ({{ s.source }}{% if not s.human %}, recorded by CodeIt{% endif %})
{{ s.text }}

{% endfor %}
</signals>
