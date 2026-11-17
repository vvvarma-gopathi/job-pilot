from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

from .ai import (
    GeminiEvaluationError,
    GeminiJobEvaluator,
    JobEvaluation,
    calculate_final_score,
    evaluation_cache_key,
)
from .config import Settings, load_search_config
from .db import JobRepository
from .http import HttpFetcher
from .matcher import JobMatcher
from .models import Job, SourceRunResult
from .sources import (
    ATSBoard,
    ATSBoardDiscovery,
    ATSBoardSource,
    ArbeitnowSource,
    RemotiveSource,
    TalivyAdapter,
)
from .sources.talivy import TalivySearchClient

logger = logging.getLogger("jobpilot.discovery")


class DiscoverySource(Protocol):
    name: str

    def fetch_jobs(self) -> list[Job]: ...


def _source_is_due(repo: JobRepository, source: str, interval_hours: int) -> bool:
    last = repo.source_last_success(source)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= interval_hours * 3600


def _remotive_is_due(repo: JobRepository, interval_hours: int) -> bool:
    return _source_is_due(repo, "remotive", interval_hours)


def run_discovery(
    source_names: set[str] | None = None,
    *,
    discover_companies: bool = False,
    talivy_client: TalivySearchClient | None = None,
) -> list[SourceRunResult]:
    settings = Settings.from_env()
    search_config = load_search_config(settings.search_config_path)
    repo = JobRepository(settings.db_path)
    matcher = JobMatcher(search_config, settings.min_match_score)
    fetcher = HttpFetcher(settings)
    ats_discovery = ATSBoardDiscovery(repo, fetcher, settings.user_agent)
    results: list[SourceRunResult] = []
    sources: list[DiscoverySource] = []

    def selected(name: str) -> bool:
        return source_names is None or name in source_names

    if selected("arbeitnow") and _source_is_due(repo, "arbeitnow", settings.arbeitnow_min_interval_hours):
        sources.append(ArbeitnowSource(fetcher, settings.arbeitnow_max_pages))
    elif selected("arbeitnow"):
        results.append(SourceRunResult(source="arbeitnow", skipped=True))
        logger.info("Skipping Arbeitnow: its configured fetch interval has not elapsed.")
    if selected("remotive") and _remotive_is_due(repo, settings.remotive_min_interval_hours):
        sources.append(RemotiveSource(fetcher))
    elif selected("remotive"):
        results.append(SourceRunResult(source="remotive", skipped=True))
        logger.info("Skipping Remotive: its configured fetch interval has not elapsed.")

    for provider in ("greenhouse", "lever"):
        if not selected(provider):
            continue
        ats_discovery.revalidate_expired(settings.ats_min_interval_hours, provider)
        boards = repo.valid_ats_boards(provider)
        if not boards:
            if source_names is not None and provider in source_names:
                results.append(SourceRunResult(
                    source=provider,
                    skipped=True,
                    warnings=["No validated boards have been discovered for this provider."],
                ))
            continue
        if not _source_is_due(repo, provider, settings.ats_min_interval_hours):
            results.append(SourceRunResult(source=provider, skipped=True))
            continue
        sources.extend(
            ATSBoardSource(fetcher, ATSBoard.model_validate(board))
            for board in boards
        )

    talivy_adapter = None
    talivy_requested = discover_companies or (
        source_names is not None and "talivy" in source_names
    ) or (source_names is None and talivy_client is not None)
    if talivy_requested:
        if talivy_client is None:
            results.append(SourceRunResult(
                source="talivy",
                skipped=True,
                warnings=["Talivy integration is disabled until its API URL, authentication, and response contract are supplied."],
            ))
        else:
            talivy_config = search_config.get("talivy", {})
            talivy_adapter = TalivyAdapter(
                talivy_client,
                repo,
                search_config,
                max_queries=talivy_config.get("max_queries", 10),
                max_results_per_query=talivy_config.get("max_results_per_query", 20),
                cache_ttl_hours=talivy_config.get("cache_ttl_hours", 24),
                request_delay_seconds=talivy_config.get("request_delay_seconds", 1.0),
            )
            if selected("talivy"):
                sources.append(talivy_adapter)
            if discover_companies:
                company_run_id = repo.start_discovery_run(
                    "talivy_company_discovery", len(talivy_adapter.queries)
                )
                try:
                    lead_tuples = [
                        (lead.company_name, str(lead.career_page_url), lead.discovery_source)
                        for lead in talivy_adapter.career_page_leads()
                    ]
                    failed_queries = talivy_adapter.failed_queries
                    discovered_boards, failed_leads = ats_discovery.discover(lead_tuples)
                    known_boards = {
                        (source.board.provider, source.board.board_identifier)
                        for source in sources if isinstance(source, ATSBoardSource)
                    }
                    for board in discovered_boards:
                        key = (board.provider, board.board_identifier)
                        if key not in known_boards:
                            sources.append(ATSBoardSource(fetcher, board))
                            known_boards.add(key)
                    repo.finish_discovery_run(
                        company_run_id,
                        status="completed",
                        jobs_discovered=len(lead_tuples),
                        jobs_valid=len(discovered_boards),
                        failed_requests=failed_queries + failed_leads,
                    )
                    logger.info(
                        "Talivy company discovery: %d career leads, %d validated boards, %d failed requests",
                        len(lead_tuples), len(discovered_boards), failed_queries + failed_leads,
                    )
                except Exception as exc:
                    repo.finish_discovery_run(
                        company_run_id, status="failed", failed_requests=1, error=str(exc)
                    )
                    results.append(SourceRunResult(
                        source="talivy", failed=True, error=f"Company discovery failed: {exc}"
                    ))
                    logger.exception("Talivy company discovery failed")

    try:
        for source in sources:
            query_count = len(getattr(source, "queries", []))
            run_id = repo.start_discovery_run(source.name, query_count)
            try:
                fetched_jobs = source.fetch_jobs()
                to_save = []
                relevant = 0
                filtered = 0
                for job in fetched_jobs:
                    result = matcher.evaluate(job)
                    if not result.relevant:
                        filtered += 1
                    job.match_score = result.score
                    job.matched_role = result.matched_role
                    job.experience_status = result.experience_status
                    job.match_reasons = result.reasons
                    job.eligibility_status = "eligible" if result.relevant else "rejected"
                    job.rejection_reason = result.rejection_reason
                    job.processing_status = "pending_ai" if result.relevant else "rejected"
                    if result.relevant:
                        relevant += 1
                    to_save.append(job)
                counts = repo.save_jobs(to_save)
                completed = datetime.now(timezone.utc).isoformat()
                failed_requests = getattr(source, "failed_queries", 0)
                repo.mark_source_success(source.name, completed)
                if isinstance(source, ATSBoardSource):
                    repo.mark_board_fetch(
                        source.board.provider, source.board.board_identifier, completed
                    )
                repo.finish_discovery_run(
                    run_id,
                    status="completed",
                    jobs_discovered=len(fetched_jobs),
                    jobs_valid=relevant,
                    duplicates=counts["updated"],
                    failed_requests=failed_requests,
                )
                results.append(SourceRunResult(source=source.name, fetched=len(fetched_jobs), relevant=relevant,
                                               filtered_out=filtered, inserted=counts["inserted"],
                                               updated=counts["updated"],
                                               warnings=[
                                                   f"{failed_requests} search query(s) failed"
                                               ] if failed_requests else []))
            except Exception as exc:
                repo.finish_discovery_run(
                    run_id, status="failed", failed_requests=1, error=str(exc)
                )
                logger.exception("Source %s failed", source.name)
                results.append(SourceRunResult(source=source.name, failed=True, error=str(exc)))
    finally:
        fetcher.close()
    return results


def run_evaluation() -> dict[str, int]:
    settings = Settings.from_env()
    search_config = load_search_config(settings.search_config_path)
    repo = JobRepository(settings.db_path)
    pending = repo.pending_ai_jobs()
    if not pending:
        return {"pending": 0, "evaluated": 0, "failed": 0}
    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY is not configured; eligible jobs remain pending AI evaluation.")
        return {"pending": len(pending), "evaluated": 0, "failed": 0}

    try:
        evaluator = GeminiJobEvaluator(
            settings.gemini_api_key, settings.gemini_model, settings.gemini_max_retries
        )
    except RuntimeError as exc:
        logger.error("Gemini evaluator is unavailable; jobs remain pending: %s", exc)
        return {"pending": len(pending), "evaluated": 0, "failed": len(pending)}
    evaluated = failed = 0
    locations = search_config.get("locations", ["India", "Remote"])
    for job in pending:
        key = evaluation_cache_key(job, settings.gemini_model, search_config)
        try:
            cached = repo.get_cached_evaluation(key)
            if cached is not None:
                evaluation = JobEvaluation.model_validate(cached)
            else:
                evaluation = evaluator.evaluate(job, search_config)
                evaluation = calculate_final_score(
                    evaluation, job["location"], job["workplace_type"],
                    job["employment_type"], locations,
                )
                repo.cache_evaluation(key, evaluation.model_dump())
            repo.update_ai_evaluation(
                job["id"], evaluation.model_dump(), evaluation.overall_score, evaluation.missing_skills
            )
            evaluated += 1
        except GeminiEvaluationError as exc:
            failed += 1
            logger.error("Gemini evaluation failed for job %s: %s", job["id"], exc)
            if exc.status_code == 429:
                logger.warning("Gemini rate limit/quota reached; remaining jobs will stay pending.")
                break
        except Exception:
            failed += 1
            logger.exception("Could not evaluate job %s; it remains pending.", job["id"])
    return {"pending": len(pending) - evaluated, "evaluated": evaluated, "failed": failed}
