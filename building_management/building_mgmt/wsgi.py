import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "building_mgmt.settings")  # <-- same name
application = get_wsgi_application()