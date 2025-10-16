import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "building_mgmt.settings")  # <-- same name
application = get_asgi_application()