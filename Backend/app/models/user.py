from typing import Optional
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(primary_key=True)
    email: str
    name: Optional[str] = None


    def __repr__(self):
        return "<User(id='%s', email='%s', name='%s')>" % ( self.id, self.email, self.name)