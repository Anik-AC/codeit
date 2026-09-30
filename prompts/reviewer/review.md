Review this pull request for ticket {{ key }}.

<ticket>
{{ ticket_md }}
</ticket>

<checks>
{{ checks_md }}
</checks>
{% if suggestions %}

The human sent this ticket back for more testing. Their suggestions:

<suggestions>
{% for s in suggestions %}
- {{ s }}
{% endfor %}
</suggestions>
{% endif %}

{% if file_summaries %}
The diff is large, so here are per-file summaries instead of the full diff:

<diff>
{{ file_summaries }}
</diff>
{% else %}
<diff>
{{ diff }}
</diff>
{% endif %}

Your answer:
- `ac_coverage` lists every acceptance criterion in the ticket, one entry each.
- `findings` lists every problem you found, each with a severity. A problem you mention
  in `summary_md` must also be a finding.
- The verdict follows from those two lists, as the rules say.
