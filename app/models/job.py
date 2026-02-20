"""Job-related database models."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON, ForeignKey, Float
from sqlalchemy.orm import relationship
from app.core.database import Base


class Job(Base):
    """Migration job model."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    celery_task_id = Column(String(255), unique=True, index=True)
    
    # Job configuration
    vault_url = Column(String(500))
    vault_name = Column(String(255))
    export_method = Column(String(50))
    jql = Column(Text, nullable=True)
    project_key = Column(String(50), nullable=True)
    
    # Export settings
    sharepoint_site = Column(String(500), nullable=True)
    sharepoint_folder = Column(String(500), nullable=True)
    api_type = Column(String(50), nullable=True)
    environment = Column(String(50), nullable=True)
    custom_api_url = Column(String(500), nullable=True)
    
    # Job status
    status = Column(String(50), default="pending", index=True)
    total_issues = Column(Integer, default=0)
    processed_issues = Column(Integer, default=0)
    successful_issues = Column(Integer, default=0)
    failed_issues = Column(Integer, default=0)
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Results
    result_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # Settings used
    parallelism = Column(Integer, default=5)
    send_email = Column(Boolean, default=False)
    email = Column(String(255), nullable=True)
    config_data = Column(JSON, nullable=True)  # Full config snapshot
    
    # Relationships
    logs = relationship("JobLog", back_populates="job", cascade="all, delete-orphan")
    progress_updates = relationship("JobProgress", back_populates="job", cascade="all, delete-orphan")

    def to_dict(self):
        """Convert job to dictionary."""
        return {
            "id": self.id,
            "celery_task_id": self.celery_task_id,
            "vault_url": self.vault_url,
            "vault_name": self.vault_name,
            "export_method": self.export_method,
            "jql": self.jql,
            "project_key": self.project_key,
            "sharepoint_site": self.sharepoint_site,
            "sharepoint_folder": self.sharepoint_folder,
            "api_type": self.api_type,
            "environment": self.environment,
            "custom_api_url": self.custom_api_url,
            "status": self.status,
            "total_issues": self.total_issues,
            "processed_issues": self.processed_issues,
            "successful_issues": self.successful_issues,
            "failed_issues": self.failed_issues,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
            "parallelism": self.parallelism,
        }


class JobLog(Base):
    """Log entries for migration jobs."""
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), index=True)
    
    # Log details
    issue_key = Column(String(255), index=True)
    status = Column(String(50), index=True)  # success, failed, skipped
    message = Column(Text, nullable=True)
    error_details = Column(Text, nullable=True)
    
    # Identifier info
    extracted_identifier = Column(String(100), nullable=True)
    
    # File info
    pdf_generated = Column(Boolean, default=False)
    attachments_count = Column(Integer, default=0)
    files_uploaded = Column(Integer, default=0)
    
    # Timing
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    
    # Retry info
    retry_count = Column(Integer, default=0)
    can_retry = Column(Boolean, default=True)
    
    # Relationship
    job = relationship("Job", back_populates="logs")

    def to_dict(self):
        """Convert log to dictionary."""
        return {
            "id": self.id,
            "job_id": self.job_id,
            "issue_key": self.issue_key,
            "status": self.status,
            "message": self.message,
            "error_details": self.error_details,
            "extracted_identifier": self.extracted_identifier,
            "pdf_generated": self.pdf_generated,
            "attachments_count": self.attachments_count,
            "files_uploaded": self.files_uploaded,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "retry_count": self.retry_count,
            "can_retry": self.can_retry,
        }


class JobProgress(Base):
    """Real-time progress updates for jobs."""
    __tablename__ = "job_progress"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), index=True)
    
    # Progress info
    current_issue = Column(String(255), nullable=True)
    current_step = Column(String(100), nullable=True)
    progress_percent = Column(Float, default=0.0)
    message = Column(Text, nullable=True)
    
    # Timing
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    job = relationship("Job", back_populates="progress_updates")

    def to_dict(self):
        """Convert progress to dictionary."""
        return {
            "id": self.id,
            "job_id": self.job_id,
            "current_issue": self.current_issue,
            "current_step": self.current_step,
            "progress_percent": self.progress_percent,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
