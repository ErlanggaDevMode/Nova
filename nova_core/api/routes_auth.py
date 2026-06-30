from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from nova_core.auth import verify_password, create_access_token

router = APIRouter()

class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/auth/token")
async def login(payload: LoginRequest):
    if not verify_password(payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token({"sub": payload.username})
    return {"access_token": access_token, "token_type": "bearer"}
