from fastapi import FastAPI, Request, Depends, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import models
from database import engine, SessionLocal, Base

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================
# Login Routes
# =========================
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(username=username, password=password).first()
    if user:
        response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        response.set_cookie(key="user", value=user.username)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})

# =========================
# Home Route
# =========================
@app.get("/", response_class=HTMLResponse)
def read_items(request: Request, db: Session = Depends(get_db)):
    user = request.cookies.get("user")
    if not user:
        return RedirectResponse(url="/login")
    items = db.query(models.Item).all()
    return templates.TemplateResponse("index.html", {"request": request, "items": items, "user": user})

# =========================
# Add Item (for testing)
# =========================
@app.get("/add/{name}/{description}")
def add_item(name: str, description: str, db: Session = Depends(get_db)):
    item = models.Item(name=name, description=description)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"message": "Item added", "item": {"name": name, "description": description}}

# =========================
# Create a test user (run once)
# =========================
@app.get("/create_test_user")
def create_test_user(db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(username="admin").first()
    if not user:
        new_user = models.User(username="admin", password="password")
        db.add(new_user)
        db.commit()
        return {"message": "Test user created: admin / password"}
    return {"message": "Test user already exists"}
