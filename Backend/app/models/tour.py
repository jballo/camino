"""Structured tour artifact — the contract for guided-tour generation."""

from pydantic import BaseModel, Field, model_validator


class TourStep(BaseModel):
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    snippet: str = Field(min_length=1)
    why: str | None = None

    @model_validator(mode="after")
    def end_not_before_start(self) -> "TourStep":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class TourArtifact(BaseModel):
    title: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    repo_name: str = Field(min_length=1)
    steps: list[TourStep] = Field(min_length=1)
