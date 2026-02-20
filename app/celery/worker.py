import os
import tempfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import argparse
from contextlib import contextmanager
from typing import Dict, Any
from datetime import datetime
import traceback

from celery import shared_task
from celery.utils.log import get_task_logger
from jinja2 import Environment, FileSystemLoader

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.constants import DEFAULT_IDENTIFIER_FIELD_TERMS, DEFAULT_IMPORTANT_FIELD_TERMS, DEFAULT_PARALLELISM
from app.models.job import Job, JobLog, JobProgress

# Set up Jinja2 template environment for email templates
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates', 'email')
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)

logger = get_task_logger(__name__)


@contextmanager
def get_db_session():
    """Context manager for database sessions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def update_job_status(job_db_id: int, status: str, **kwargs):
    """Update job status in database."""
    with get_db_session() as db:
        job = db.query(Job).filter(Job.id == job_db_id).first()
        if job:
            job.status = status
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)


def add_job_log(job_db_id: int, issue_key: str, status: str, **kwargs):
    """Add a log entry for an issue."""
    with get_db_session() as db:
        log = JobLog(
            job_id=job_db_id,
            issue_key=issue_key,
            status=status,
            started_at=datetime.utcnow(),
            **kwargs
        )
        db.add(log)


def add_progress_update(job_db_id: int, current_issue: str, progress_percent: float, message: str, step: str = None):
    """Add a progress update."""
    with get_db_session() as db:
        progress = JobProgress(
            job_id=job_db_id,
            current_issue=current_issue,
            current_step=step,
            progress_percent=progress_percent,
            message=message,
            timestamp=datetime.utcnow()
        )
        db.add(progress)

        # Also update the job's processed count
        job = db.query(Job).filter(Job.id == job_db_id).first()
        if job:
            job.processed_issues = int(job.total_issues * progress_percent / 100) if job.total_issues else 0


def record_issue_result(job_db_id: int, issue_key: str, processed: int, total: int,
                        status: str, error: str = None, extracted_identifier: str = None,
                        pdf_generated: bool = False, attachments_count: int = 0, files_uploaded: int = 0):
    """Record issue result: progress update, log entry, and job counters in a single DB session."""
    db = SessionLocal()
    try:
        progress_percent = (processed / total * 100) if total > 0 else 0
        now = datetime.utcnow()

        # 1. Add progress update
        progress = JobProgress(
            job_id=job_db_id,
            current_issue=issue_key,
            current_step="migrating",
            progress_percent=progress_percent,
            message=f"Processing {issue_key} ({processed}/{total})",
            timestamp=now
        )
        db.add(progress)

        # 2. Add log entry
        log = JobLog(
            job_id=job_db_id,
            issue_key=issue_key,
            status=status,
            message=f"Processed {issue_key}",
            error_details=error,
            extracted_identifier=extracted_identifier,
            pdf_generated=pdf_generated,
            attachments_count=attachments_count,
            files_uploaded=files_uploaded,
            started_at=now,
            completed_at=now
        )
        db.add(log)

        # 3. Update job counters
        job = db.query(Job).filter(Job.id == job_db_id).first()
        if job:
            if (job.total_issues or 0) == 0 and total > 0:
                job.total_issues = total
            job.processed_issues = processed
            if status == 'success':
                job.successful_issues = (job.successful_issues or 0) + 1
            elif status == 'failed':
                job.failed_issues = (job.failed_issues or 0) + 1

        # Single commit for all operations
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording issue result for {issue_key}: {e}")
    finally:
        db.close()


@shared_task(bind=True)
def process_migration(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process a Jira migration task.
    
    Args:
        task_data: Dictionary containing migration parameters
        
    Returns:
        Dictionary with migration results
    """
    logger.info(f"Starting migration job {self.request.id}")
    job_db_id = task_data.get('job_db_id')
    
    # Update job status to started
    if job_db_id:
        update_job_status(job_db_id, 'started', started_at=datetime.utcnow())
    
    try:
        # Import here to avoid circular imports
        from jiramigration.sharepoint_cmd import JiraMigration
        
        # Create args for JiraMigration
        args = argparse.Namespace()
        
        # Set required fields
        args.vault_url = task_data.get('vault_url', settings.JIRA_URL)
        args.vault_name = task_data.get('vault_name', settings.JIRA_USERNAME)
        args.vault_token = task_data.get('vault_token', settings.JIRA_TOKEN)
        args.export_method = task_data.get('export_method', 'sharepoint')
        args.parallelism = task_data.get('parallelism', DEFAULT_PARALLELISM)
        
        # Set optional fields
        if task_data.get('jql'):
            args.jql = task_data['jql']
        
        if task_data.get('project_key'):
            args.project_key = task_data['project_key']
            
        # Total issues count (known for CSV up front; for JQL set from first progress callback)
        total_issues_count = None
        # Handle CSV data if provided
        if task_data.get('csv_data'):
            # Filter out any issues that should be skipped (already successful)
            skip_issues = set(task_data.get('skip_issues', []))
            issues_to_process = [k for k in task_data['csv_data'] if k not in skip_issues]
            total_issues_count = len(issues_to_process)
            
            if issues_to_process:
                # Create temporary CSV file
                with tempfile.NamedTemporaryFile(delete=False, suffix='.csv') as temp_file:
                    temp_path = temp_file.name
                    # Write header
                    temp_file.write(b"key\n")
                    # Write issue keys
                    for issue_key in issues_to_process:
                        temp_file.write(f"{issue_key}\n".encode())
                
                args.csv_file = temp_path
                logger.info(f"Processing {len(issues_to_process)} issues (skipping {len(skip_issues)} already successful)")
            else:
                args.csv_file = None
                logger.info("No issues to process after filtering successful ones")
        else:
            args.csv_file = None
        
        # Store skip_issues for JQL-based queries
        args.skip_issues = set(task_data.get('skip_issues', []))
            
        # Set export method specific fields
        if args.export_method == 'sharepoint':
            args.sharepoint_site = task_data.get('sharepoint_site', settings.SHAREPOINT_SITE)
            args.sharepoint_folder = task_data.get('sharepoint_folder')
        elif args.export_method == 'api':
            args.api_type = task_data.get('api_type', 'custom')
            args.custom_api_url = task_data.get('custom_api_url')
            args.custom_api_headers = task_data.get('custom_api_headers', {})
            args.custom_api_params = task_data.get('custom_api_params', {})
        
        # Set custom fields options
        args.analyze_fields = task_data.get('analyze_fields', True)
        args.show_all_fields = task_data.get('show_all_fields', False)
        args.use_view_screen = task_data.get('use_view_screen', True)
        args.identifier_field_terms = task_data.get('identifier_field_terms', DEFAULT_IDENTIFIER_FIELD_TERMS)
        args.important_field_terms = task_data.get('important_field_terms', DEFAULT_IMPORTANT_FIELD_TERMS)
        args.include_manual_fields = task_data.get('include_manual_fields', False)
        args.manual_fields = task_data.get('manual_fields', [])
        
        # Update job status to running (set total_issues now for CSV so UI shows count from the start)
        if job_db_id:
            kwargs = {}
            if total_issues_count is not None:
                kwargs['total_issues'] = total_issues_count
            update_job_status(job_db_id, 'running', **kwargs)
        
        # Create JiraMigration instance and run migration
        migration = JiraMigration(args)
        
        # Set up progress callback if job_db_id is available
        if job_db_id:
            def progress_callback(issue_key, processed, total, status, error=None, extracted_identifier=None,
                                  pdf_generated=False, attachments_count=0, files_uploaded=0):
                """Callback for migration progress updates."""
                record_issue_result(
                    job_db_id, issue_key, processed, total,
                    status, error=error, extracted_identifier=extracted_identifier,
                    pdf_generated=pdf_generated, attachments_count=attachments_count,
                    files_uploaded=files_uploaded
                )
            
            # Try to set progress callback on migration object
            if hasattr(migration, 'set_progress_callback'):
                migration.set_progress_callback(progress_callback)
        
        migration_result = migration.start_migration()
        
        # Clean up temporary CSV file if it was created
        if task_data.get('csv_data') and args.csv_file and os.path.exists(args.csv_file):
            os.unlink(args.csv_file)
        
        # Update job status to completed
        if job_db_id:
            # Parse migration result to get final counts
            result_data = migration_result if isinstance(migration_result, dict) else {"raw": str(migration_result)}
            update_job_status(
                job_db_id, 
                'completed',
                completed_at=datetime.utcnow(),
                result_data=result_data
            )
            
        # Update task state
        self.update_state(state='SUCCESS')
        
        # Return result
        return {
            "status": "completed",
            "job_id": self.request.id,
            "db_job_id": job_db_id,
            "details": migration_result
        }
        
    except Exception as e:
        logger.error(f"Error in migration job {self.request.id}: {e}")
        logger.error(traceback.format_exc())
        
        # Update job status to failed
        if job_db_id:
            update_job_status(
                job_db_id,
                'failed',
                completed_at=datetime.utcnow(),
                error_message=str(e)
            )
        
        # Update task state
        self.update_state(state='FAILURE', meta={"error": str(e)})
        # Raise exception to mark task as failed
        raise e
        

@shared_task(bind=True)
def process_confluence_migration(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process a Confluence migration task.

    Args:
        task_data: Dictionary containing Confluence migration parameters

    Returns:
        Dictionary with migration results
    """
    logger.info(f"Starting Confluence migration job {self.request.id}")
    job_db_id = task_data.get('job_db_id')

    if job_db_id:
        update_job_status(job_db_id, 'started', started_at=datetime.utcnow())

    try:
        from jiramigration.confluence_cmd import ConfluenceMigration

        # Build args namespace
        args = argparse.Namespace()
        args.confluence_url = task_data.get('confluence_url')
        args.confluence_username = task_data.get('confluence_username')
        args.confluence_token = task_data.get('confluence_token')
        args.space_key = task_data.get('space_key')
        args.cql = task_data.get('cql')
        args.sharepoint_site = task_data.get('sharepoint_site')
        args.sharepoint_folder = task_data.get('sharepoint_folder')
        args.parallelism = task_data.get('parallelism', 5)
        args.skip_issues = set(task_data.get('skip_issues', []))

        if job_db_id:
            update_job_status(job_db_id, 'running')

        migration = ConfluenceMigration(args)

        if job_db_id:
            def progress_callback(issue_key, processed, total, status, error=None,
                                  extracted_identifier=None, pdf_generated=False,
                                  attachments_count=0, files_uploaded=0):
                record_issue_result(
                    job_db_id, issue_key, processed, total,
                    status, error=error, extracted_identifier=extracted_identifier,
                    pdf_generated=pdf_generated, attachments_count=attachments_count,
                    files_uploaded=files_uploaded
                )

            migration.set_progress_callback(progress_callback)

        migration_result = migration.start_migration()

        if job_db_id:
            result_data = migration_result if isinstance(migration_result, dict) else {"raw": str(migration_result)}
            update_job_status(
                job_db_id,
                'completed',
                completed_at=datetime.utcnow(),
                result_data=result_data
            )

        self.update_state(state='SUCCESS')

        return {
            "status": "completed",
            "job_id": self.request.id,
            "db_job_id": job_db_id,
            "details": migration_result
        }

    except Exception as e:
        logger.error(f"Error in Confluence migration job {self.request.id}: {e}")
        logger.error(traceback.format_exc())

        if job_db_id:
            update_job_status(
                job_db_id,
                'failed',
                completed_at=datetime.utcnow(),
                error_message=str(e)
            )

        self.update_state(state='FAILURE', meta={"error": str(e)})
        raise e

@shared_task
def send_email_report(migration_result: Dict[str, Any], recipient_email: str) -> Dict[str, Any]:
    """
    Send an email report of the migration results.

    Args:
        migration_result: Results from process_migration task
        recipient_email: Email address to send the report to

    Returns:
        Dictionary with email sending results
    """
    logger.info(f"Sending email report to {recipient_email}")

    try:
        # Create email message
        msg = MIMEMultipart()
        msg['From'] = settings.EMAIL_FROM
        msg['To'] = recipient_email
        msg['Subject'] = f"Migration Report - Job {migration_result['job_id']}"

        # Render email body from template
        template = jinja_env.get_template('migration_report.html')
        body = template.render(
            job_id=migration_result['job_id'],
            status=migration_result['status'],
            details=migration_result.get('details', {})
        )

        msg.attach(MIMEText(body, 'html'))

        # Connect to SMTP server and send email
        with smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)

        return {
            "status": "sent",
            "recipient": recipient_email,
            "job_id": migration_result['job_id']
        }

    except Exception as e:
        logger.error(f"Error sending email report: {e}")
        # Raise exception to mark task as failed
        raise e 