from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler

from playwright.sync_api import Error as PlaywrightError

from .automation import ApplicationPreparer, BrowserManager
from .automation.exceptions import AutomationSafetyError
from .agent import (
    AgentAlreadyRunning,
    AgentOrchestrator,
    AgentScheduler,
    SingleInstanceLock,
)
from .config import AutomationSettings, Settings
from .db import APPLICATION_STATUSES, JobRepository
from .gmail import GmailIntegrationError, GmailReadOnlyClient
from .mail import GeminiEmailClassifier, RecruiterResponseProcessor
from .notifications import WindowsNotifier
from .runner import run_discovery, run_evaluation


def _show_jobs(jobs: list[dict], *, rejected: bool = False) -> None:
    if not jobs:
        print("No jobs found.")
        return
    for job in jobs:
        print(f"[{job['match_score']}%] {job['title']} | {job['company']} | "
              f"{job['location'] or 'Location not specified'}")
        print(f"  Source: {job['source'] or 'unknown'} | Role: {job['matched_role'] or 'not matched'} "
              f"| Experience: {job['experience_status']}")
        print(f"  Application: {job['apply_url']}")
        if job["source_url"]:
            print(f"  Listing / attribution: {job['source_url']}")
        if rejected:
            print(f"  Rejection reason: {job['rejection_reason'] or 'Not specified'}")
        else:
            print(f"  AI status: {job['processing_status']}")
            print("  Reasons: " + "; ".join(json.loads(job["match_reasons_json"] or "[]")))
            if job["missing_skills_json"] != "[]":
                print("  Missing skills: " + ", ".join(json.loads(job["missing_skills_json"])))


def _show_applications(applications: list[dict]) -> None:
    if not applications:
        print("No applications found.")
        return
    for application in applications:
        print(
            f"[{application['application_status']}] #{application['id']} "
            f"{application['job_title']} | {application['company_name']}"
        )
        print(f"  Job ID: {application['job_id']} | Source: {application['source'] or 'unknown'}")
        print(f"  Application: {application['application_url']}")
        if application["applied_at"]:
            print(f"  Applied: {application['applied_at']}")


def _csv_safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _export_applications(repo: JobRepository, destination: Path) -> int:
    rows = repo.application_export_rows()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        fields = list(rows[0])
    else:
        fields = [
            "id", "job_id", "company_name", "job_title", "application_url", "source",
            "applied_at", "application_status", "resume_used", "cover_letter_used",
            "submission_reference", "submission_verified", "recruiter_email",
            "last_updated_at", "notes", "error_message",
        ]
    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({
            key: _csv_safe(value) for key, value in row.items()
        } for row in rows)
    return len(rows)


def _show_application(application: dict) -> None:
    print(f"Application #{application['id']}: {application['job_title']} | {application['company_name']}")
    print(f"Status: {application['application_status']}")
    print(f"Application URL: {application['application_url']}")
    print(f"Source: {application['source'] or 'unknown'} | Job ID: {application['job_id']}")
    print(f"Submission verified: {'yes' if application['submission_verified'] else 'no'}")
    if application["applied_at"]:
        print(f"Applied at: {application['applied_at']}")
    if application["submission_reference"]:
        print(f"Submission reference: {application['submission_reference']}")
    if application["recruiter_email"]:
        print(f"Recruiter email: {application['recruiter_email']}")
    if application["error_message"]:
        print(f"Last error: {application['error_message']}")
    for event in application["events"]:
        print(f"  {event['created_at']} {event['event_type']}: {event['detail'] or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description="JobPilot AI - local job discovery and evaluation")
    parser.add_argument(
        "command",
        choices=[
            "discover", "evaluate", "run", "stats", "recent", "rejected", "pending",
            "companies", "sources", "discovery-stats", "applications", "application",
            "gmail", "recruiter-emails",
            "agent",
        ],
        nargs="?",
        default="discover",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=[
            "add", "show", "retry", "mark", "stats", "export", "prepare", "auth",
            "sync", "run", "run-once", "status",
        ],
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--source", choices=["remotive", "arbeitnow", "talivy", "greenhouse", "lever"])
    parser.add_argument("--discover-companies", action="store_true")
    parser.add_argument("--status", choices=APPLICATION_STATUSES)
    parser.add_argument("--id", type=int)
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-possible-duplicate", action="store_true")
    parser.add_argument("--confirm-submission", action="store_true")
    parser.add_argument("--browser-channel")
    parser.add_argument(
        "--email-status", choices=["matched", "unmatched", "review", "ignored"]
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    repo = JobRepository(settings.db_path)

    if args.command in {"discover", "run"}:
        selected_sources = {args.source} if args.source else None
        results = run_discovery(
            selected_sources,
            discover_companies=args.discover_companies,
        )
        for result in results:
            status = "SKIPPED" if result.skipped else ("FAILED" if result.failed else "OK")
            print(f"[{status}] {result.source}: fetched={result.fetched}, relevant={result.relevant}, "
                  f"inserted={result.inserted}, updated={result.updated}, filtered={result.filtered_out}")
            if result.error:
                print(f"  error: {result.error}")
            for warning in result.warnings:
                print(f"  note: {warning}")
        print(f"Shortlisted jobs in database: {repo.count_jobs()}")
    if args.command in {"evaluate", "run"}:
        result = run_evaluation()
        print(f"AI evaluation: evaluated={result['evaluated']}, pending={result['pending']}, "
              f"failed={result['failed']}")
    elif args.command == "stats":
        print(f"Shortlisted jobs: {repo.count_jobs()}")
        print(f"Evaluation statuses: {json.dumps(repo.count_by_status(), sort_keys=True)}")
        print("Sources:")
        for source in repo.source_stats():
            print(f"  {source['source']}: total={source['total']}, relevant={source['relevant']}, "
                  f"rejected={source['rejected']}")
        print(f"Database: {settings.db_path.resolve()}")
    elif args.command == "recent":
        _show_jobs(repo.recent_jobs(args.limit))
    elif args.command == "rejected":
        _show_jobs(repo.rejected_jobs(args.limit), rejected=True)
    elif args.command == "pending":
        _show_jobs(repo.pending_ai_jobs(args.limit))
    elif args.command == "applications":
        _show_applications(repo.applications(status=args.status, limit=args.limit))
    elif args.command == "application":
        if args.action == "add":
            if args.job_id is None:
                parser.error("application add requires --job-id")
            try:
                result = repo.create_application(
                    args.job_id,
                    confirm_possible_duplicate=args.confirm_possible_duplicate,
                )
            except (KeyError, ValueError) as exc:
                parser.error(str(exc))
            if result["created"]:
                print(f"Created application #{result['application_id']} in shortlisted status.")
            elif result["duplicate"]:
                print(f"Duplicate application record: #{result['application_id']}; no new record created.")
            else:
                possible = ", ".join(str(value) for value in result["possible_duplicate_ids"])
                print(f"Possible same-company/title application(s): {possible}.")
                print("Review them before retrying with --confirm-possible-duplicate.")
        elif args.action == "show":
            if args.id is None:
                parser.error("application show requires --id")
            application = repo.application(args.id)
            if application is None:
                parser.error(f"Application {args.id} does not exist")
            _show_application(application)
        elif args.action == "retry":
            if args.id is None:
                parser.error("application retry requires --id")
            try:
                repo.retry_application(args.id)
            except (KeyError, ValueError) as exc:
                parser.error(str(exc))
            print(f"Application #{args.id} moved to pending_review; verify its prior submission before proceeding.")
        elif args.action == "prepare":
            if args.id is None:
                parser.error("application prepare requires --id")

            def confirm_submission(report, page) -> bool:
                application = repo.application(report.application_id)
                if application is not None:
                    print(
                        f"Review employer: {application['company_name']} | "
                        f"{application['job_title']}"
                    )
                    print(f"Application URL: {application['application_url']}")
                print(f"Current browser page: {page.url}")
                print("Review the open browser and complete any safe manual fields.")
                print("Type SUBMIT to submit this application; anything else cancels.")
                return input("> ").strip() == "SUBMIT"

            browser_channel = args.browser_channel or os.getenv("PLAYWRIGHT_BROWSER_CHANNEL")
            browser_factory = lambda: BrowserManager(
                headless=False, browser_channel=browser_channel
            )
            try:
                preparer = ApplicationPreparer.from_settings(
                    repo,
                    settings,
                    AutomationSettings.from_env(),
                    browser_factory=browser_factory,
                )
                report = preparer.prepare(
                    args.id,
                    confirm_submit=confirm_submission,
                )
            except (AutomationSafetyError, KeyError, ValueError, PlaywrightError) as exc:
                parser.error(str(exc))
            print(f"Preparation status: {report.status}")
            print("Filled fields: " + (", ".join(report.fill_result.filled_fields) or "none"))
            if report.missing_required_fields:
                print("Required fields needing attention: " + ", ".join(report.missing_required_fields))
            if report.blocked_fields:
                print("Safety/manual review: " + "; ".join(report.blocked_fields))
            if report.submission is not None:
                print(f"Submission verified: {report.submission.verified}")
                if report.submission.reference:
                    print(f"Reference: {report.submission.reference}")
                print(report.submission.reason)
        elif args.action == "mark":
            if args.id is None or args.status is None:
                parser.error("application mark requires --id and --status")
            try:
                repo.update_application_status(
                    args.id,
                    args.status,
                    submission_verified=args.confirm_submission,
                )
            except (KeyError, ValueError) as exc:
                parser.error(str(exc))
            print(f"Application #{args.id} status updated to {args.status}.")
        elif args.action == "stats":
            stats = repo.application_stats()
            print("Application counts:")
            if not stats:
                print("  No applications recorded.")
            for status, count in sorted(stats.items()):
                print(f"  {status}: {count}")
        elif args.action == "export":
            destination = args.output or (Path("data") / "applications.csv")
            count = _export_applications(repo, destination)
            print(f"Exported {count} application(s) to {destination.resolve()}")
        else:
            parser.error("application requires one of: add, show, retry, mark, stats, export")
    elif args.command == "gmail":
        automation_settings = AutomationSettings.from_env()
        client = GmailReadOnlyClient(
            settings.gmail_oauth_client_file,
            query=automation_settings.gmail_query,
            max_results=automation_settings.gmail_max_messages,
        )
        if args.action == "auth":
            try:
                client.authorize(interactive=True)
            except (GmailIntegrationError, FileNotFoundError) as exc:
                parser.error(str(exc))
            print("Gmail read-only authorization stored in Windows Credential Manager.")
        elif args.action == "sync":
            if not automation_settings.gmail_enabled:
                parser.error("Set GMAIL_ENABLED=true before syncing Gmail.")
            if (
                automation_settings.gemini_email_classification_enabled
                and not settings.gemini_api_key
            ):
                parser.error(
                    "Gemini email classification is enabled but GEMINI_API_KEY is not configured."
                )
            try:
                client.authorize()
                messages = client.fetch_messages()
                gemini_classifier = None
                if automation_settings.gemini_email_classification_enabled:
                    gemini_classifier = GeminiEmailClassifier(
                        settings.gemini_api_key or "", settings.gemini_model
                    )
                processor = RecruiterResponseProcessor(
                    repo,
                    notifier=WindowsNotifier(
                        enabled=automation_settings.notifications_enabled
                    ),
                    gemini_classifier=gemini_classifier,
                )
                counts = processor.process(messages)
            except (GmailIntegrationError, FileNotFoundError, ValueError) as exc:
                parser.error(str(exc))
            print(
                f"Gmail messages: processed={counts['processed']}, matched={counts['matched']}, "
                f"unmatched={counts['unmatched']}, duplicates={counts['duplicates']}"
            )
        else:
            parser.error("gmail requires one of: auth, sync")
    elif args.command == "recruiter-emails":
        emails = repo.recruiter_emails(
            processing_status=args.email_status,
            limit=args.limit,
        )
        if not emails:
            print("No recruiter emails found.")
        for email in emails:
            print(
                f"[{email['processing_status']}] {email['classification']} "
                f"({email['confidence']:.2f})"
            )
            print(f"  From: {email['sender']} | Subject: {email['subject']}")
            if email["company_name"]:
                print(f"  Application: {email['job_title']} | {email['company_name']}")
            else:
                print("  Application: unmatched")
            print(f"  Received: {email['received_at'] or 'unknown'}")
    elif args.command == "agent":
        automation_settings = AutomationSettings.from_env()
        lock_path = settings.db_path.parent / "agent.lock"
        stop_file = settings.db_path.parent / "agent.stop"
        if args.action == "status":
            print(f"Agent running: {'yes' if SingleInstanceLock.is_locked(lock_path) else 'no'}")
            tasks = repo.agent_tasks(limit=args.limit)
            if not tasks:
                print("No agent tasks recorded.")
            for task in tasks:
                print(
                    f"[{task['status']}] {task['task_type']} #{task['task_id']} "
                    f"retries={task['retry_count']}/{task['max_retries']}"
                )
                if task["error_details"]:
                    print(f"  {task['error_details']}")
        elif args.action in {"run", "run-once"}:
            notifier = WindowsNotifier(enabled=automation_settings.notifications_enabled)
            orchestrator = AgentOrchestrator(
                repo, settings, automation_settings, notifier=notifier
            )
            scheduler = AgentScheduler(orchestrator, stop_file)
            try:
                with SingleInstanceLock(lock_path):
                    if stop_file.exists():
                        stop_file.unlink()
                    if args.action == "run-once":
                        summary = scheduler.run_once()
                        for task_type, outcome in summary["outcomes"].items():
                            print(f"[{outcome.status}] {task_type}")
                    else:
                        log_directory = Path("logs")
                        log_directory.mkdir(parents=True, exist_ok=True)
                        file_handler = RotatingFileHandler(
                            log_directory / "agent.log",
                            maxBytes=2_000_000,
                            backupCount=3,
                            encoding="utf-8",
                        )
                        file_handler.setFormatter(logging.Formatter(
                            "%(asctime)s %(levelname)s %(name)s: %(message)s"
                        ))
                        logging.getLogger().addHandler(file_handler)
                        print("JobPilot local agent started. Stop it with scripts\\stop_agent.ps1.")
                        scheduler.run_forever()
            except AgentAlreadyRunning as exc:
                parser.error(str(exc))
            except KeyboardInterrupt:
                print("JobPilot local agent stopped.")
        else:
            parser.error("agent requires one of: run, run-once, status")
    elif args.command == "companies":
        companies = repo.companies()
        boards = repo.company_boards()
        if not companies:
            print("No companies discovered yet.")
        for company in companies:
            print(f"{company['name']}: {company['board_count']} board(s), "
                  f"{company['valid_boards']} validated")
            for board in boards:
                if board["company_name"] == company["name"]:
                    print(f"  {board['provider']}:{board['board_identifier']} "
                          f"[{board['validation_status']}] {board['career_page_url']}")
    elif args.command == "sources":
        print("remotive: enabled (public API)")
        print("arbeitnow: enabled (public API)")
        for provider in ("greenhouse", "lever"):
            boards = repo.valid_ats_boards(provider)
            print(f"{provider}: {'enabled' if boards else 'waiting for validated boards'} "
                  f"({len(boards)} validated)")
        print("talivy: disabled (API contract required; no endpoint or auth guessed)")
    elif args.command == "discovery-stats":
        runs = repo.discovery_stats(args.limit)
        if not runs:
            print("No discovery runs recorded yet.")
        for run in runs:
            print(f"{run['source']}: runs={run['runs']}, discovered={run['jobs_discovered'] or 0}, "
                  f"valid={run['jobs_valid'] or 0}, duplicates={run['duplicates'] or 0}, "
                  f"failed_requests={run['failed_requests'] or 0}, "
                  f"last={run['last_completed_at'] or 'running'}")


if __name__ == "__main__":
    main()
