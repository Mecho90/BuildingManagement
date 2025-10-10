from pydantic import BaseModel

class UserCreate(BaseModel):
    username: str
    password: str

class TaskCreate(BaseModel):
    title: str

class TaskResponse(BaseModel):
    id: int
    title: str
    completed: bool
    owner_id: int

    class Config:
        orm_mode = True