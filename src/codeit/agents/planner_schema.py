"""The Planner's output contract (PRD 11.1), validated with Pydantic."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SPLIT_POINTS = 5  # stories above this are flagged `split-me`
MIN_CRITERIA = 2  # stories with fewer acceptance criteria are flagged `split-me`


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TestPlan(_Strict):
    unit: list[str] = Field(default_factory=list)
    e2e: list[str] = Field(default_factory=list)


class StoryDraft(_Strict):
    ref: Annotated[str, Field(pattern=r"^S\d+$", description="S1, S2, ... unique in the plan")]
    summary: Annotated[
        str, Field(min_length=1, max_length=80, description="Imperative, at most 80 characters")
    ]
    user_story: Annotated[str, Field(min_length=1, description="As a ..., I want ..., so that ...")]
    acceptance_criteria: Annotated[
        list[Annotated[str, Field(min_length=1)]],
        Field(min_length=1, description="Given ... When ... Then ..."),
    ]
    technical_notes_md: str = ""
    test_plan: TestPlan
    suggested_points: Annotated[int, Field(ge=1, le=13)]
    depends_on: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"]

    @property
    def needs_split(self) -> bool:
        return self.suggested_points > SPLIT_POINTS or len(self.acceptance_criteria) < MIN_CRITERIA


class EpicDraft(_Strict):
    summary: Annotated[str, Field(min_length=1, max_length=255)]
    description_md: str = ""


class PlanDraft(_Strict):
    epic: EpicDraft
    stories: Annotated[list[StoryDraft], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _check_refs(self) -> PlanDraft:
        refs = [s.ref for s in self.stories]
        dupes = sorted({r for r in refs if refs.count(r) > 1})
        if dupes:
            raise ValueError(f"duplicate story refs: {', '.join(dupes)}")
        known = set(refs)
        for s in self.stories:
            unknown = [d for d in s.depends_on if d not in known]
            if unknown:
                raise ValueError(f"{s.ref} depends on unknown refs: {', '.join(unknown)}")
            if s.ref in s.depends_on:
                raise ValueError(f"{s.ref} depends on itself")
        cycle = _find_cycle({s.ref: s.depends_on for s in self.stories})
        if cycle:
            raise ValueError(f"dependency cycle: {' -> '.join(cycle)}")
        return self


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = on the stack, 2 = done
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for dep in graph[node]:
            if state.get(dep) == 1:
                return [*stack[stack.index(dep) :], dep]
            if dep not in state and (found := visit(dep)):
                return found
        stack.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state and (found := visit(node)):
            return found
    return None


def plan_json_schema() -> dict[str, Any]:
    """The schema sent to the model, with the story and epic definitions inlined."""
    return PlanDraft.model_json_schema()
