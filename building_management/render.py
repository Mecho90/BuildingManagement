from django.template import loader
from starlette.responses import HTMLResponse

def render_template(name: str, context: dict | None = None, status_code: int = 200) -> HTMLResponse:
    tmpl = loader.get_template(name)
    return HTMLResponse(tmpl.render(context or {}), status_code=status_code)