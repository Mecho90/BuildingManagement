# Translation Tooling

## django-rosetta

1. Install in your virtualenv:
   ```bash
   pip install django-rosetta
   ```
2. Add `"rosetta"` to `INSTALLED_APPS` **after** Django’s contrib apps.
3. Include Rosetta URLs for staff:
   ```python
   from django.urls import path, include

   urlpatterns += [path("rosetta/", include("rosetta.urls"))]
   ```
4. Restrict access via `ROSETTA_ACCESS_CONTROL_FUNCTION` in `settings.py`, returning `user.is_staff`.
5. Usage: navigate to `/rosetta/`, select a locale, edit strings, then click “Save & Compile”.

## CI Commands

Run after migrations/static builds:
```bash
python manage.py makemessages -l en -l bg --check
python manage.py compilemessages --check
```

Ensure `gettext` binaries (`msgfmt`, `xgettext`) are present in the CI image.
