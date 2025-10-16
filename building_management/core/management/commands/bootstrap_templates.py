"""
Generates the HTML templates so the project runs end-to-end while keeping this
delivery 100% Python. Run:

    python manage.py bootstrap_templates

Why: You asked for "python code format" only, but views need templates.
"""
from __future__ import annotations

from pathlib import Path
from django.core.management.base import BaseCommand


BASE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 1.5rem; }
      header { margin-bottom: 1rem; }
      nav a { margin-right: 1rem; }
      table { border-collapse: collapse; width: 100%; }
      th, td { border: 1px solid #ddd; padding: .5rem; }
      .btn { display: inline-block; padding: .4rem .7rem; border: 1px solid #aaa; border-radius: .4rem; text-decoration: none; }
      .btn:hover { background: #f6f6f6; }
      form input, form select, form textarea { width: 100%; padding: .5rem; margin: .25rem 0 .75rem; }
    </style>
  </head>
  <body>
    <header>
      <nav>
        <a href="/buildings/">Buildings</a>
        <a href="/units/">Units</a>
        <a href="/work-orders/">Work Orders</a>
        <a href="/admin/">Admin</a>
      </nav>
      <hr/>
    </header>
    {% block content %}{% endblock %}
  </body>
</html>
"""

BUILDINGS_LIST = """{% extends "base.html" %}
{% block content %}
<h2>Buildings</h2>
<table>
  <thead><tr><th>Name</th><th>Address</th><th>Units</th></tr></thead>
  <tbody>
  {% for b in buildings %}
    <tr>
      <td><a href="{% url 'building_detail' b.id %}">{{ b.name }}</a></td>
      <td>{{ b.address }}</td>
      <td>{{ b.units.count }}</td>
    </tr>
  {% empty %}
    <tr><td colspan="3">No buildings yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

BUILDING_DETAIL = """{% extends "base.html" %}
{% block content %}
<h2>{{ building.name }}</h2>
<p><strong>Address:</strong> {{ building.address }}</p>
<h3>Units</h3>
<table>
  <thead><tr><th>#</th><th>Floor</th><th>Occupied</th></tr></thead>
  <tbody>
  {% for u in building.units.all %}
    <tr>
      <td>{{ u.number }}</td>
      <td>{{ u.floor }}</td>
      <td>{{ u.is_occupied }}</td>
    </tr>
  {% empty %}
    <tr><td colspan="3">No units.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

UNITS_LIST = """{% extends "base.html" %}
{% block content %}
<h2>Units</h2>
<table>
  <thead><tr><th>Building</th><th>#</th><th>Floor</th><th>Occupied</th></tr></thead>
  <tbody>
  {% for u in units %}
    <tr>
      <td>{{ u.building.name }}</td>
      <td>{{ u.number }}</td>
      <td>{{ u.floor }}</td>
      <td>{{ u.is_occupied }}</td>
    </tr>
  {% empty %}
    <tr><td colspan="4">No units.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

WORK_ORDERS_LIST = """{% extends "base.html" %}
{% block content %}
<h2>Work Orders</h2>
<p><a class="btn" href="{% url 'work_order_create' %}">New Work Order</a></p>
<table>
  <thead><tr><th>Title</th><th>Unit</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
  <tbody>
  {% for w in work_orders %}
    <tr>
      <td>{{ w.title }}</td>
      <td>{{ w.unit.building.name }} #{{ w.unit.number }}</td>
      <td>{{ w.get_status_display }}</td>
      <td>{{ w.created_at }}</td>
      <td><a class="btn" href="{% url 'work_order_update' w.id %}">Edit</a></td>
    </tr>
  {% empty %}
    <tr><td colspan="5">No work orders.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

WORK_ORDER_FORM = """{% extends "base.html" %}
{% block content %}
<h2>{% if form.instance.pk %}Edit{% else %}New{% endif %} Work Order</h2>
<form method="post">
  {% csrf_token %}
  {{ form.as_p }}
  <button class="btn" type="submit">Save</button>
</form>
{% endblock %}
"""


class Command(BaseCommand):
    help = "Create HTML templates used by views (kept as Python strings here)."

    def handle(self, *args, **options):
        base_dir = Path.cwd() / "templates"
        (base_dir / "core").mkdir(parents=True, exist_ok=True)

        files = {
            base_dir / "base.html": BASE,
            base_dir / "core" / "buildings_list.html": BUILDINGS_LIST,
            base_dir / "core" / "building_detail.html": BUILDING_DETAIL,
            base_dir / "core" / "units_list.html": UNITS_LIST,
            base_dir / "core" / "work_orders_list.html": WORK_ORDERS_LIST,
            base_dir / "core" / "l": WORK_ORDER_FORM,
        }

        for path, content in files.items():
            if not path.exists():
                path.write_text(content, encoding="utf-8")
                self.stdout.write(self.style.SUCCESS(f"Created {path}"))
            else:
                self.stdout.write(self.style.WARNING(f"Exists  {path}"))

        self.stdout.write(self.style.SUCCESS("Templates ready."))