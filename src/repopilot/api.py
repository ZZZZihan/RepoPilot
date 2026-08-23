"""FastAPI transport for RepoPilot's planning and approval interface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from repopilot.adapters.github import GitHubRepositoryInspector
from repopilot.config import Settings
from repopilot.errors import RepoPilotError
from repopilot.inspection import RepositoryInspector
from repopilot.models import (
    ApprovePlanRequest,
    CreatePlanRequest,
    HealthResponse,
    ImplementationPlan,
)
from repopilot.planning import PlanningService
from repopilot.storage import SQLitePlanStore


def create_app(
    *,
    settings: Settings | None = None,
    inspector: RepositoryInspector | None = None,
    store: SQLitePlanStore | None = None,
) -> FastAPI:
    """Build an application with explicit adapters for production or contract tests."""

    active_settings = settings or Settings.from_environment()
    active_store = store or SQLitePlanStore(active_settings.database_path)
    active_inspector = inspector or GitHubRepositoryInspector(
        limits=active_settings.inspection_limits,
        token=active_settings.github_token,
        api_version=active_settings.github_api_version,
    )
    planning = PlanningService(inspector=active_inspector, store=active_store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(active_store.initialize)
        yield

    application = FastAPI(
        title="RepoPilot",
        summary="Evidence-backed implementation plans for small Python repositories",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(RepoPilotError)
    async def handle_repopilot_error(_: Request, exc: RepoPilotError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @application.get("/healthz", response_model=HealthResponse, tags=["operations"])
    async def healthcheck() -> HealthResponse:
        await asyncio.to_thread(active_store.healthcheck)
        return HealthResponse()

    @application.post(
        "/v1/plans",
        response_model=ImplementationPlan,
        status_code=status.HTTP_201_CREATED,
        tags=["plans"],
    )
    async def create_plan(request: CreatePlanRequest) -> ImplementationPlan:
        """Inspect a bounded GitHub snapshot and persist a proposed plan."""

        return await planning.create_plan(request)

    @application.get("/v1/plans/{plan_id}", response_model=ImplementationPlan, tags=["plans"])
    async def get_plan(plan_id: UUID) -> ImplementationPlan:
        """Read the authoritative persisted plan document."""

        return await planning.get_plan(plan_id)

    @application.post(
        "/v1/plans/{plan_id}/approval",
        response_model=ImplementationPlan,
        tags=["plans"],
    )
    async def approve_plan(plan_id: UUID, request: ApprovePlanRequest) -> ImplementationPlan:
        """Transition exactly one expected proposed-plan version to approved."""

        return await planning.approve_plan(plan_id, request)

    @application.get(
        "/v1/schemas/implementation-plan",
        response_model=dict[str, Any],
        tags=["schemas"],
    )
    async def implementation_plan_schema() -> dict[str, Any]:
        """Expose the same JSON Schema enforced at construction, persistence, and reads."""

        return ImplementationPlan.model_json_schema(mode="validation")

    return application


app = create_app()
