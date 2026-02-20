"""Job management API endpoints."""
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import Optional, List
from datetime import datetime, timedelta
import csv
import io

from app.core.database import get_db
from app.models.job import Job, JobLog, JobProgress
from app.celery.worker import process_migration, send_email_report

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/")
async def list_jobs(
    status: Optional[str] = None,
    export_method: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """List migration jobs with optional filtering."""
    query = db.query(Job)
    
    if status:
        query = query.filter(Job.status == status)
    if export_method:
        query = query.filter(Job.export_method == export_method)
    
    total = query.count()
    jobs = query.order_by(desc(Job.created_at)).offset(offset).limit(limit).all()
    
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "jobs": [job.to_dict() for job in jobs]
    }


@router.get("/stats")
async def get_job_stats(
    days: int = Query(default=30, le=365),
    db: Session = Depends(get_db)
):
    """Get job statistics for dashboard."""
    since = datetime.utcnow() - timedelta(days=days)
    
    # Total counts
    total_jobs = db.query(Job).filter(Job.created_at >= since).count()
    completed_jobs = db.query(Job).filter(
        Job.created_at >= since, 
        Job.status == "completed"
    ).count()
    failed_jobs = db.query(Job).filter(
        Job.created_at >= since, 
        Job.status == "failed"
    ).count()
    running_jobs = db.query(Job).filter(
        Job.status.in_(["pending", "started", "running"])
    ).count()
    
    # Issue stats
    total_issues = db.query(func.sum(Job.total_issues)).filter(
        Job.created_at >= since
    ).scalar() or 0
    successful_issues = db.query(func.sum(Job.successful_issues)).filter(
        Job.created_at >= since
    ).scalar() or 0
    failed_issues = db.query(func.sum(Job.failed_issues)).filter(
        Job.created_at >= since
    ).scalar() or 0
    
    # By export method
    by_method = db.query(
        Job.export_method, 
        func.count(Job.id)
    ).filter(
        Job.created_at >= since
    ).group_by(Job.export_method).all()
    
    # Recent jobs
    recent = db.query(Job).order_by(desc(Job.created_at)).limit(5).all()
    
    return {
        "period_days": days,
        "jobs": {
            "total": total_jobs,
            "completed": completed_jobs,
            "failed": failed_jobs,
            "running": running_jobs,
            "success_rate": round(completed_jobs / total_jobs * 100, 1) if total_jobs > 0 else 0
        },
        "issues": {
            "total": total_issues,
            "successful": successful_issues,
            "failed": failed_issues,
            "success_rate": round(successful_issues / total_issues * 100, 1) if total_issues > 0 else 0
        },
        "by_export_method": {method: count for method, count in by_method},
        "recent_jobs": [job.to_dict() for job in recent]
    }


@router.get("/{job_id}")
async def get_job(job_id: int, db: Session = Depends(get_db)):
    """Get a specific job by ID."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return job.to_dict()


@router.get("/by-celery/{celery_task_id}")
async def get_job_by_celery_id(celery_task_id: str, db: Session = Depends(get_db)):
    """Get a job by Celery task ID."""
    job = db.query(Job).filter(Job.celery_task_id == celery_task_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return job.to_dict()


@router.get("/{job_id}/logs")
async def get_job_logs(
    job_id: int,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get logs for a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    query = db.query(JobLog).filter(JobLog.job_id == job_id)
    
    if status:
        query = query.filter(JobLog.status == status)
    
    if search:
        query = query.filter(JobLog.issue_key.ilike(f"%{search}%"))
    
    total = query.count()
    logs = query.order_by(JobLog.started_at).offset(offset).limit(limit).all()
    
    return {
        "job_id": job_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "logs": [log.to_dict() for log in logs]
    }


@router.get("/{job_id}/logs/failed")
async def get_failed_logs(
    job_id: int, 
    limit: int = Query(default=20, le=200),
    db: Session = Depends(get_db)
):
    """Get failed issue logs for retry functionality."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    query = db.query(JobLog).filter(
        JobLog.job_id == job_id,
        JobLog.status == "failed",
        JobLog.can_retry == True
    )
    
    total_failed = query.count()
    logs = query.limit(limit).all()
    
    return {
        "job_id": job_id,
        "failed_count": total_failed,
        "logs": [log.to_dict() for log in logs]
    }


@router.get("/{job_id}/logs/export")
async def export_job_logs(
    job_id: int,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Export all logs for a job as CSV."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    query = db.query(JobLog).filter(JobLog.job_id == job_id)
    if status:
        query = query.filter(JobLog.status == status)
    
    logs = query.order_by(JobLog.started_at).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Issue Key', 'Status', 'Extracted Identifier', 'PDF Generated', 'Attachments', 'Files Uploaded', 'Duration (s)', 'Message', 'Error'])
    
    for log in logs:
        writer.writerow([
            log.issue_key,
            log.status,
            log.extracted_identifier or '',
            log.pdf_generated,
            log.attachments_count,
            log.files_uploaded,
            f"{log.duration_seconds:.1f}" if log.duration_seconds else '',
            log.message or '',
            log.error_details or ''
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=migration_logs_job_{job_id}.csv"}
    )


@router.get("/{job_id}/progress")
async def get_job_progress(job_id: int, db: Session = Depends(get_db)):
    """Get the latest progress for a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    progress = db.query(JobProgress).filter(
        JobProgress.job_id == job_id
    ).order_by(desc(JobProgress.timestamp)).first()
    
    return {
        "job_id": job_id,
        "status": job.status,
        "total_issues": job.total_issues,
        "processed_issues": job.processed_issues,
        "successful_issues": job.successful_issues,
        "failed_issues": job.failed_issues,
        "latest_progress": progress.to_dict() if progress else None
    }


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, db: Session = Depends(get_db)):
    """Cancel a running job."""
    from app.celery.celery_app import celery_app
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Only allow cancelling running jobs
    if job.status not in ["pending", "started", "running"]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel job with status: {job.status}")
    
    # Revoke the Celery task
    if job.celery_task_id:
        celery_app.control.revoke(job.celery_task_id, terminate=True, signal='SIGTERM')
    
    # Update job status
    job.status = "cancelled"
    job.completed_at = datetime.utcnow()
    job.error_message = "Job cancelled by user"
    db.commit()
    
    return {
        "message": "Job cancelled successfully",
        "job_id": job_id,
        "celery_task_id": job.celery_task_id
    }


@router.delete("/{job_id}")
async def delete_job(job_id: int, db: Session = Depends(get_db)):
    """Delete a job and its logs."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Don't delete running jobs
    if job.status in ["pending", "started", "running"]:
        raise HTTPException(status_code=400, detail="Cannot delete a running job. Cancel it first.")
    
    db.delete(job)
    db.commit()
    
    return {"message": "Job deleted successfully"}


@router.post("/{job_id}/restart")
async def restart_job(job_id: int, db: Session = Depends(get_db)):
    """Restart a failed job from scratch with the same configuration."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Only allow restart for failed or completed jobs
    if job.status in ["pending", "started", "running"]:
        raise HTTPException(status_code=400, detail="Cannot restart a running job")
    
    if not job.config_data:
        raise HTTPException(status_code=400, detail="Job configuration not available for restart")
    
    # Create a new job with the same configuration
    task_data = job.config_data.copy()
    
    new_job = Job(
        vault_url=job.vault_url,
        vault_name=job.vault_name,
        export_method=job.export_method,
        jql=job.jql,
        project_key=job.project_key,
        sharepoint_site=job.sharepoint_site,
        sharepoint_folder=job.sharepoint_folder,
        api_type=job.api_type,
        environment=job.environment,
        custom_api_url=job.custom_api_url,
        status="pending",
        parallelism=job.parallelism,
        send_email=job.send_email,
        email=job.email,
        config_data=task_data,
        created_at=datetime.utcnow()
    )
    db.add(new_job)
    db.commit()
    db.refresh(new_job)
    
    # Update task data with new job ID
    task_data["job_db_id"] = new_job.id
    
    # Queue the task in Celery
    if new_job.export_method == "confluence":
        from app.celery.worker import process_confluence_migration
        if new_job.send_email and new_job.email:
            result = process_confluence_migration.apply_async(
                args=[task_data],
                link=send_email_report.s(new_job.email)
            )
        else:
            result = process_confluence_migration.apply_async(args=[task_data])
    else:
        if new_job.send_email and new_job.email:
            result = process_migration.apply_async(
                args=[task_data],
                link=send_email_report.s(new_job.email)
            )
        else:
            result = process_migration.apply_async(args=[task_data])

    # Update job with Celery task ID
    new_job.celery_task_id = result.id
    db.commit()

    return {
        "message": "Job restarted successfully",
        "original_job_id": job_id,
        "new_job_id": new_job.id,
        "celery_task_id": result.id
    }


@router.post("/{job_id}/resume")
async def resume_job(job_id: int, db: Session = Depends(get_db)):
    """
    Resume a failed job, processing only issues that weren't successfully completed.
    This includes:
    - Failed issues (that can be retried)
    - Unprocessed issues (if job crashed before processing all)
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Only allow resume for failed or completed jobs
    if job.status in ["pending", "started", "running"]:
        raise HTTPException(status_code=400, detail="Cannot resume a running job")
    
    if not job.config_data:
        raise HTTPException(status_code=400, detail="Job configuration not available for resume")
    
    # Get successfully processed issue keys - these will be skipped
    successful_logs = db.query(JobLog).filter(
        JobLog.job_id == job_id,
        JobLog.status == "success"
    ).all()
    successful_keys = {log.issue_key for log in successful_logs}
    
    # Get failed issue keys that can be retried
    failed_logs = db.query(JobLog).filter(
        JobLog.job_id == job_id,
        JobLog.status == "failed",
        JobLog.can_retry == True
    ).all()
    failed_keys = [log.issue_key for log in failed_logs]
    
    # Create a new job with modified configuration
    task_data = job.config_data.copy()
    original_config = job.config_data.copy()
    
    # Calculate issues to process
    issues_to_process = []
    unprocessed_count = 0

    if job.export_method == "confluence":
        # For Confluence jobs, set skip_issues to successful page titles.
        # ConfluenceMigration will filter these out after fetching pages.
        task_data["skip_issues"] = list(successful_keys)
        total_to_retry = len(failed_keys)
        if job.total_issues and job.processed_issues and job.total_issues > job.processed_issues:
            unprocessed_count = job.total_issues - job.processed_issues
            total_to_retry += unprocessed_count

        if total_to_retry == 0:
            raise HTTPException(status_code=400, detail="No pages to retry - all pages were successful")
    else:
        # Jira-specific resume logic
        # Check if original job had specific issue keys (csv_data)
        if original_config.get("csv_data"):
            # Original job had a specific list of issues
            original_issues = set(original_config["csv_data"])
            # Issues to process = original list minus successful ones
            issues_to_process = [k for k in original_config["csv_data"] if k not in successful_keys]
            unprocessed_count = len(original_issues) - len(successful_keys) - len(failed_keys)
        else:
            # Original job used JQL - we'll re-run with the same JQL but skip successful issues
            # Add failed keys to csv_data for immediate retry,
            # and keep the JQL for any issues that weren't even fetched
            if failed_keys:
                issues_to_process = failed_keys

            # If there were unprocessed issues (total > processed), we need to re-run JQL
            if job.total_issues and job.processed_issues and job.total_issues > job.processed_issues:
                unprocessed_count = job.total_issues - job.processed_issues
                # Keep the JQL to re-fetch and process remaining issues
                task_data["skip_issues"] = list(successful_keys)
            elif failed_keys:
                # All issues were processed, just retry the failed ones
                task_data["csv_data"] = failed_keys
                task_data["jql"] = None

        # If we have specific issues to process, use csv_data
        if issues_to_process:
            task_data["csv_data"] = issues_to_process
            if not unprocessed_count or unprocessed_count <= 0:
                # No unprocessed issues from JQL, just use the specific list
                task_data["jql"] = None

        # Always set skip_issues to avoid re-processing successful ones
        task_data["skip_issues"] = list(successful_keys)

        total_to_retry = len(failed_keys) + max(0, unprocessed_count)

        if total_to_retry == 0 and not task_data.get("jql"):
            raise HTTPException(status_code=400, detail="No issues to retry - all issues were successful")

    task_data["resume_from_job_id"] = job_id
    
    # Build description for the resumed job
    resume_desc_parts = []
    if failed_keys:
        resume_desc_parts.append(f"{len(failed_keys)} failed")
    if unprocessed_count > 0:
        resume_desc_parts.append(f"~{unprocessed_count} unprocessed")
    resume_desc = ", ".join(resume_desc_parts) if resume_desc_parts else "remaining issues"
    
    new_job = Job(
        vault_url=job.vault_url,
        vault_name=job.vault_name,
        export_method=job.export_method,
        jql=job.jql if job.export_method == "confluence" else f"Resumed from Job #{job_id} - {resume_desc}",
        project_key=job.project_key,
        sharepoint_site=job.sharepoint_site,
        sharepoint_folder=job.sharepoint_folder,
        api_type=job.api_type,
        environment=job.environment,
        custom_api_url=job.custom_api_url,
        status="pending",
        parallelism=job.parallelism,
        send_email=job.send_email,
        email=job.email,
        config_data=task_data,
        created_at=datetime.utcnow()
    )
    db.add(new_job)
    db.commit()
    db.refresh(new_job)
    
    # Update task data with new job ID
    task_data["job_db_id"] = new_job.id
    
    # Queue the task in Celery
    if new_job.export_method == "confluence":
        from app.celery.worker import process_confluence_migration
        if new_job.send_email and new_job.email:
            result = process_confluence_migration.apply_async(
                args=[task_data],
                link=send_email_report.s(new_job.email)
            )
        else:
            result = process_confluence_migration.apply_async(args=[task_data])
    else:
        if new_job.send_email and new_job.email:
            result = process_migration.apply_async(
                args=[task_data],
                link=send_email_report.s(new_job.email)
            )
        else:
            result = process_migration.apply_async(args=[task_data])

    # Update job with Celery task ID
    new_job.celery_task_id = result.id
    db.commit()

    return {
        "message": "Job resumed successfully",
        "original_job_id": job_id,
        "new_job_id": new_job.id,
        "celery_task_id": result.id,
        "failed_issues": len(failed_keys),
        "unprocessed_issues": max(0, unprocessed_count),
        "issues_skipped": len(successful_keys),
        "total_to_process": total_to_retry if total_to_retry > 0 else "remaining from JQL"
    }
